import os
import socket
import threading
import time
import logging
import tkinter as tk
import customtkinter as ctk
from typing import Optional

import sounddevice as sd
import yaml
from pynput import keyboard

# === Configure logging ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# === Load configuration from .env ===
# Load configuration from YAML
import sys

# Get the directory where the executable (or script) is located
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    app_dir = os.path.dirname(sys.executable)
else:
    # Running as script
    app_dir = os.path.dirname(__file__)

config_path = os.path.join(app_dir, "config.yml")
with open(config_path, "r") as f:
    config = yaml.safe_load(f) or {}

PORT = int(config.get("P2T_PORT", 5007))
PEER_IP = config.get("P2T_PEER_IP", "127.0.0.1")
RETRY_SECONDS = int(config.get("P2T_RETRY_SECONDS", 30))
PTT_KEY = config.get("PTT_KEY", "space")

# === Audio parameters ===
SAMPLE_RATE = 16000  # Hz
CHANNELS = 1         # mono
CHUNK = 1024         # frames per block

# === Connection parameters ===
SOCKET_TIMEOUT = 30.0
KEEPALIVE_INTERVAL = 10
KEEPALIVE_IDLE = 30
KEEPALIVE_PROBES = 3

# === Shared state ===
connection_lock = threading.Lock()
connection: Optional[socket.socket] = None
connected_event = threading.Event()
stop_event = threading.Event()
listener_socket: Optional[socket.socket] = None

# === PTT state ===
ptt_active = threading.Event()  # Set when ANY PTT trigger is active
ptt_lock = threading.Lock()
kb_ptt_held = threading.Event()  # Keyboard PTT held
ui_ptt_held = threading.Event()  # UI button PTT held

# === UI state ===
ui_is_connected = threading.Event()  # Accurate connection status for UI


def update_ptt_state() -> None:
    """Update ptt_active based on all input sources. Must be called with ptt_lock held."""
    if kb_ptt_held.is_set() or ui_ptt_held.is_set():
        if not ptt_active.is_set():
            ptt_active.set()
            logger.info("🎤 PTT ACTIVE - Transmitting")
    else:
        if ptt_active.is_set():
            ptt_active.clear()
            logger.info("🔇 PTT RELEASED - Muted")


def configure_socket(sock: socket.socket) -> None:
    """Apply optimal socket configuration for low-latency audio."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        
        if hasattr(socket, 'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
        if hasattr(socket, 'TCP_KEEPINTVL'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
        if hasattr(socket, 'TCP_KEEPCNT'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_PROBES)
            
        sock.settimeout(SOCKET_TIMEOUT)
        logger.debug("Socket configured")
    except OSError as e:
        logger.warning(f"Could not set all socket options: {e}")


def safe_close_socket(sock: Optional[socket.socket]) -> None:
    """Safely close a socket with proper shutdown."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def get_local_ip() -> str:
    """Get the local IP address used for outgoing connections."""
    try:
        # Create a UDP socket to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((PEER_IP, PORT))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "0.0.0.0"


def should_use_incoming(peer_ip: str) -> bool:
    """
    Tie-breaker for simultaneous connections.
    Keep connection from peer with LOWER IP address.
    This ensures both sides make the same decision.
    """
    try:
        local_ip = get_local_ip()
        # Compare IPs as integers
        peer_int = sum([int(x) * (256 ** i) for i, x in enumerate(reversed(peer_ip.split('.')))])
        local_int = sum([int(x) * (256 ** i) for i, x in enumerate(reversed(local_ip.split('.')))])
        return peer_int < local_int
    except Exception:
        # If comparison fails, just use the incoming connection
        return True


def send_audio(sock: socket.socket) -> None:
    """
    Reads audio from microphone ONLY when PTT is active and sends it over socket.
    When PTT is inactive, sends silence.
    """
    logger.info("Audio sender ready (waiting for PTT)")
    stream = None
    bytes_per_frame = 2  # int16
    silence = b'\x00' * (CHUNK * bytes_per_frame)
    
    try:
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK,
        )
        stream.start()
        
        while not stop_event.is_set():
            try:
                if ptt_active.is_set():
                    # PTT is pressed - send real audio
                    data, overflowed = stream.read(CHUNK)
                    if overflowed:
                        logger.warning("Audio input overflow")
                    sock.sendall(data)
                else:
                    # PTT not pressed - send silence and drain microphone
                    stream.read(CHUNK)  # Drain buffer but don't use
                    sock.sendall(silence)
                
            except socket.timeout:
                logger.error("Send timeout - connection dead")
                break
            except (OSError, BrokenPipeError, ConnectionError) as e:
                logger.error(f"Send error: {e}")
                break
                
    except Exception as e:
        logger.error(f"Audio send init failed: {e}")
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                logger.warning(f"Error closing audio input: {e}")
        logger.info("Send thread exiting")


def recv_audio(sock: socket.socket) -> None:
    """
    Receives audio from socket and plays it to speakers.
    Receives continuously regardless of PTT state.
    """
    logger.info("Starting network -> speakers")
    bytes_per_frame = 2
    expected = CHUNK * bytes_per_frame
    stream = None
    
    try:
        stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK,
        )
        stream.start()
        
        while not stop_event.is_set():
            try:
                buf = b""
                
                while len(buf) < expected and not stop_event.is_set():
                    remaining = expected - len(buf)
                    try:
                        chunk = sock.recv(remaining)
                    except socket.timeout:
                        logger.error("Receive timeout - connection dead")
                        return
                        
                    if not chunk:
                        logger.info("Peer closed connection")
                        return
                    buf += chunk
                
                if stop_event.is_set():
                    break
                
                if len(buf) == expected:
                    stream.write(buf)
                else:
                    logger.warning(f"Buffer mismatch: {len(buf)} != {expected}")
                    
            except (OSError, ConnectionError) as e:
                logger.error(f"Receive error: {e}")
                break
                
    except Exception as e:
        logger.error(f"Audio receive init failed: {e}")
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                logger.warning(f"Error closing audio output: {e}")
        logger.info("Receive thread exiting")


def on_press(key) -> None:
    """Handle key press events."""
    try:
        if hasattr(key, 'name') and key.name == PTT_KEY:
            with ptt_lock:
                if not kb_ptt_held.is_set():
                    kb_ptt_held.set()
                    update_ptt_state()
        elif key == keyboard.Key.space and PTT_KEY == "space":
            with ptt_lock:
                if not kb_ptt_held.is_set():
                    kb_ptt_held.set()
                    update_ptt_state()
    except AttributeError:
        pass


def on_release(key) -> None:
    """Handle key release events."""
    try:
        # Check for Escape key to quit
        if key == keyboard.Key.esc:
            logger.info("Escape pressed - shutting down")
            stop_event.set()
            return False  # Stop listener
        
        # Check for PTT key release
        if hasattr(key, 'name') and key.name == PTT_KEY:
            with ptt_lock:
                if kb_ptt_held.is_set():
                    kb_ptt_held.clear()
                    update_ptt_state()
        elif key == keyboard.Key.space and PTT_KEY == "space":
            with ptt_lock:
                if kb_ptt_held.is_set():
                    kb_ptt_held.clear()
                    update_ptt_state()
    except AttributeError:
        pass


def keyboard_listener_thread() -> None:
    """Run keyboard listener in a thread."""
    logger.info(f"Keyboard listener started - Press and hold '{PTT_KEY.upper()}' to talk, ESC to quit")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


def listen_for_peer(port: int) -> None:
    """Listen for incoming TCP connection."""
    global connection, listener_socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener_socket = sock

    try:
        sock.bind(("0.0.0.0", port))
        sock.listen(1)
        sock.settimeout(1.0)
        logger.info(f"Listening on 0.0.0.0:{port}")
    except OSError as e:
        logger.error(f"Listener bind failed: {e}")
        safe_close_socket(sock)
        listener_socket = None
        return

    while not stop_event.is_set() and not connected_event.is_set():
        try:
            conn, addr = sock.accept()
            peer_ip = addr[0]
            logger.info(f"Incoming connection from {peer_ip}:{addr[1]}")
            
            configure_socket(conn)
            
            with connection_lock:
                if connection is None and not stop_event.is_set():
                    connection = conn
                    connected_event.set()
                    logger.info("✅ Using incoming connection")
                else:
                    # Already have a connection - apply tie-breaking rule
                    # Keep the connection from the peer with lower IP
                    if should_use_incoming(peer_ip):
                        logger.info(f"Replacing outgoing with incoming (tie-breaker: {peer_ip} < local)")
                        old_conn = connection
                        connection = conn
                        safe_close_socket(old_conn)
                        connected_event.set()
                    else:
                        safe_close_socket(conn)
                        logger.info("Keeping outgoing connection (tie-breaker)")
                    break
                    
        except socket.timeout:
            continue
        except OSError as e:
            if not stop_event.is_set():
                logger.error(f"Listener error: {e}")
            break

    safe_close_socket(sock)
    listener_socket = None
    logger.info("Listener thread exiting")


def connect_to_peer(peer_ip: str, port: int, retry_seconds: int) -> None:
    """Try to connect to peer in a loop."""
    global connection

    while not stop_event.is_set() and not connected_event.is_set():
        sock = None
        try:
            logger.info(f"Connecting to {peer_ip}:{port}")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((peer_ip, port))
            
            logger.info("Outgoing connection established")
            configure_socket(sock)

            with connection_lock:
                if connection is None and not stop_event.is_set():
                    connection = sock
                    connected_event.set()
                    logger.info("✅ Using outgoing connection")
                    return
                else:
                    # Already have a connection (probably incoming) - apply tie-breaking
                    if should_use_incoming(peer_ip):
                        # Should use incoming - close this outgoing
                        logger.info(f"Preferring incoming connection (tie-breaker: {peer_ip} < local)")
                        safe_close_socket(sock)
                    else:
                        # Should use outgoing - replace incoming with this outgoing
                        logger.info(f"Replacing incoming with outgoing (tie-breaker: local < {peer_ip})")
                        old_conn = connection
                        connection = sock
                        safe_close_socket(old_conn)
                        connected_event.set()
                    return

        except (OSError, ConnectionError) as e:
            if sock is not None:
                safe_close_socket(sock)
                
            if stop_event.is_set() or connected_event.is_set():
                break
                
            logger.info(f"Connect failed: {e}. Retrying in {retry_seconds}s...")
            
            # Sleep in small increments to respond quickly to stop_event
            for _ in range(retry_seconds):
                if stop_event.is_set() or connected_event.is_set():
                    break
                time.sleep(1.0)

    logger.info("Connector thread exiting")


def cleanup_all() -> None:
    """Clean up all resources."""
    global connection, listener_socket
    
    logger.info("Cleaning up...")
    
    with connection_lock:
        if connection is not None:
            safe_close_socket(connection)
            connection = None
    
    if listener_socket is not None:
        safe_close_socket(listener_socket)
        listener_socket = None
    
    logger.info("Cleanup complete")


def run_connection_cycle() -> bool:
    """
    Run one connection cycle (connect -> stream audio -> handle disconnect).
    Returns True if clean exit requested, False if should reconnect.
    """
    global connection, connected_event
    
    # Reset connection state
    connected_event.clear()
    with connection_lock:
        connection = None

    # Start network threads
    listener_thread = threading.Thread(
        target=listen_for_peer,
        args=(PORT,),
        daemon=True,
        name="Listener"
    )
    connector_thread = threading.Thread(
        target=connect_to_peer,
        args=(PEER_IP, PORT, RETRY_SECONDS),
        daemon=True,
        name="Connector"
    )
    
    try:
        listener_thread.start()
        connector_thread.start()
    except Exception as e:
        logger.error(f"Failed to start network threads: {e}")
        return False

    # Wait for connection
    try:
        logger.info("⏳ Waiting for peer connection...")
        while not connected_event.is_set() and not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Interrupted before connection")
        stop_event.set()
        return True

    if stop_event.is_set():
        return True

    if not connected_event.is_set():
        logger.warning("Connection attempt timed out")
        return False

    # Get connection
    with connection_lock:
        sock = connection
        if sock is None:
            logger.error("Connection is None after connected_event set")
            return False

    logger.info("")
    logger.info("=" * 60)
    logger.info("✅ CONNECTED - Ready for push-to-talk")
    logger.info(f"   Hold {PTT_KEY.upper()} to transmit")
    logger.info("   Release to stop transmitting")
    logger.info("   Press ESC to quit")
    logger.info("=" * 60)
    logger.info("")

    # Signal UI that we're connected
    ui_is_connected.set()

    # Start audio threads
    t_send = threading.Thread(target=send_audio, args=(sock,), name="AudioSend")
    t_recv = threading.Thread(target=recv_audio, args=(sock,), name="AudioRecv")
    
    try:
        t_send.start()
        t_recv.start()
    except Exception as e:
        logger.error(f"Failed to start audio threads: {e}")
        ui_is_connected.clear()
        safe_close_socket(sock)
        return False

    # Monitor audio threads
    try:
        while not stop_event.is_set():
            if not t_send.is_alive() and not t_recv.is_alive():
                logger.warning("Connection lost (audio threads stopped)")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Interrupted during call")
        stop_event.set()

    # Shutdown this connection cycle
    ui_is_connected.clear()
    logger.info("Closing connection...")
    safe_close_socket(sock)
    
    t_send.join(timeout=3.0)
    t_recv.join(timeout=3.0)
    
    if t_send.is_alive():
        logger.warning("Send thread did not exit cleanly")
    if t_recv.is_alive():
        logger.warning("Receive thread did not exit cleanly")
    
    # Return True if clean exit requested, False if should reconnect
    return stop_event.is_set()


# === UI CODE ===

# Set appearance before creating any widgets
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class PTTWindow:
    """Minimal PTT UI with round button and status indicator."""
    
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("PTT")
        root.geometry("200x200")
        root.minsize(200, 200)
        root.resizable(False, False)
        
        # Track if PTT button is being held
        self._btn_pressed = False
        
        # Colors
        self._color_idle = "#3d3d3d"
        self._color_hover = "#4a4a4a"
        self._color_pressed = "#22cc22"  # Bright green when transmitting
        self._color_connected = "#22cc22"
        self._color_disconnected = "#cc2222"
        
        # Main container frame
        self.container = ctk.CTkFrame(root, fg_color="transparent")
        self.container.pack(expand=True, fill="both", padx=15, pady=15)
        
        # PTT Button (large, round)
        self.ptt_btn = ctk.CTkButton(
            self.container,
            text="PTT",
            font=ctk.CTkFont(size=24, weight="bold"),
            width=130,
            height=130,
            corner_radius=65,  # Makes it circular
            fg_color=self._color_idle,
            hover_color=self._color_hover,
            text_color="white",
            command=None  # We use bind instead for press/release
        )
        self.ptt_btn.pack(expand=True)
        
        # Bind press and release events
        self.ptt_btn.bind("<ButtonPress-1>", self._on_ptt_press)
        self.root.bind("<ButtonRelease-1>", self._on_ptt_release)
        
        # Status indicator (small dot in upper right corner of window)
        self.status_dot = ctk.CTkLabel(
            root,
            text="●",
            font=ctk.CTkFont(size=24),
            text_color=self._color_disconnected,
            fg_color="transparent",
            width=24,
            height=24
        )
        self.status_dot.place(relx=0.88, rely=0.12, anchor="center")
        
        # Start polling for status updates
        self._update_status()
    
    def _on_ptt_press(self, event) -> None:
        """Handle PTT button press."""
        self._btn_pressed = True
        # Set both fg_color AND hover_color to prevent hover from overriding
        self.ptt_btn.configure(fg_color=self._color_pressed, hover_color=self._color_pressed)
        with ptt_lock:
            if not ui_ptt_held.is_set():
                ui_ptt_held.set()
                update_ptt_state()
    
    def _on_ptt_release(self, event) -> None:
        """Handle PTT button release (anywhere in window)."""
        if not self._btn_pressed:
            return
        self._btn_pressed = False
        # Restore both colors
        self.ptt_btn.configure(fg_color=self._color_idle, hover_color=self._color_hover)
        with ptt_lock:
            if ui_ptt_held.is_set():
                ui_ptt_held.clear()
                update_ptt_state()
    
    def _update_status(self) -> None:
        """Poll connection status and PTT state, update UI."""
        if stop_event.is_set():
            self.root.destroy()
            return
        
        # Update connection indicator
        color = self._color_connected if ui_is_connected.is_set() else self._color_disconnected
        self.status_dot.configure(text_color=color)
        
        # Update button color based on PTT state (keyboard or UI)
        if ptt_active.is_set():
            self.ptt_btn.configure(fg_color=self._color_pressed, hover_color=self._color_pressed)
        elif not self._btn_pressed:
            # Only reset if UI button isn't being held (avoid flicker)
            self.ptt_btn.configure(fg_color=self._color_idle, hover_color=self._color_hover)
        
        self.root.after(250, self._update_status)


def run_app_logic() -> None:
    """Main connection loop - runs in background thread."""
    while not stop_event.is_set():
        if not run_connection_cycle():
            if not stop_event.is_set():
                logger.info(f"Connection lost. Reconnecting in {RETRY_SECONDS}s...")
                for _ in range(RETRY_SECONDS):
                    if stop_event.is_set():
                        break
                    time.sleep(1.0)
        else:
            break


def main() -> int:
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("PUSH-TO-TALK PEER")
    logger.info("=" * 60)
    logger.info(f"Port: {PORT}")
    logger.info(f"Peer IP: {PEER_IP}")
    logger.info(f"PTT Key: {PTT_KEY.upper()}")
    logger.info(f"Retry: {RETRY_SECONDS}s")
    logger.info("=" * 60)
    
    # Check audio devices
    try:
        devices = sd.query_devices()
        logger.info(f"Audio devices found: {len(devices)}")
    except Exception as e:
        logger.error(f"Audio system unavailable: {e}")
        return 1

    # Start keyboard listener (runs for entire session)
    kb_thread = threading.Thread(
        target=keyboard_listener_thread,
        daemon=True,
        name="Keyboard"
    )
    kb_thread.start()

    # Start app logic in background thread
    app_thread = threading.Thread(
        target=run_app_logic,
        daemon=True,
        name="AppLogic"
    )
    app_thread.start()

    # Create and run UI (must be in main thread)
    root = ctk.CTk()
    ptt_window = PTTWindow(root)
    
    def on_closing():
        stop_event.set()
        root.destroy()
    
    root.protocol("WM_DELETE_WINDOW", on_closing)
    
    try:
        root.mainloop()
    except KeyboardInterrupt:
        stop_event.set()
    
    cleanup_all()
    logger.info("Exited cleanly")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
        raise SystemExit(exit_code)
    except Exception as e:
        logger.critical(f"Unexpected error: {e}", exc_info=True)
        cleanup_all()
        raise SystemExit(1)