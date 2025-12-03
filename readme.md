# 🎙️ P2T - Push-to-Talk Peer

A simple peer-to-peer push-to-talk audio application for real-time voice communication between two computers over TCP.

## Features

- **Push-to-Talk**: Hold a key to transmit, release to mute
- **Auto-reconnect**: Automatically reconnects if connection drops
- **Dual-connect**: Both peers can initiate connection simultaneously (tie-breaker handles conflicts)
- **Low latency**: Optimized socket configuration for real-time audio

## Requirements

```bash
pip install sounddevice pynput pyyaml
```

## Configuration

Create a `config.yml` file next to the script:

```yaml
P2T_PORT: 5007
P2T_PEER_IP: "192.168.1.100"  # Other PC's IP address
P2T_RETRY_SECONDS: 30
PTT_KEY: "space"              # Options: "space", "f14", etc.
```

## Usage

1. Configure `P2T_PEER_IP` on each PC to point to the other PC's IP
2. Run on both PCs:
   ```bash
   python p2t_peer_ptt.py
   ```
3. **Hold SPACE** (or configured key) to talk
4. **Press ESC** to quit

---

## 🛡️ Firewall Setup (Windows)

Run on **BOTH** PCs as Administrator.

### PowerShell (Recommended)

```powershell
# Inbound - allows peer to connect to you
New-NetFirewallRule -DisplayName "PTT Audio App (In)" -Direction Inbound -Protocol TCP -LocalPort 5007 -Action Allow -Profile Any

# Outbound - allows you to connect to peer (for strict firewalls)
New-NetFirewallRule -DisplayName "PTT Audio App (Out)" -Direction Outbound -Protocol TCP -RemotePort 5007 -Action Allow -Profile Any
```

### Command Prompt (Alternative)

```cmd
netsh advfirewall firewall add rule name="PTT Audio App (In)" dir=in action=allow protocol=TCP localport=5007 profile=any
netsh advfirewall firewall add rule name="PTT Audio App (Out)" dir=out action=allow protocol=TCP remoteport=5007 profile=any
```

### Verify Rules

```powershell
Get-NetFirewallRule -DisplayName "PTT Audio App*"
```

### Remove Rules

```powershell
Remove-NetFirewallRule -DisplayName "PTT Audio App (In)"
Remove-NetFirewallRule -DisplayName "PTT Audio App (Out)"
```

> **Note:** Outbound is typically allowed by default on Windows, but some corporate/custom firewalls may block it.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No audio devices | Check microphone/speakers are connected |
| Connection timeout | Verify IPs, firewall rules, and network connectivity |
| Audio choppy | Check network latency, close bandwidth-heavy apps |
| Key not working | Run as Administrator (pynput may need elevated privileges) |

---

## License

MIT



