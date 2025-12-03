
🛡️ FIREWALL RULE SETUP
You need to run these commands on BOTH PC A AND PC B.

⚡ METHOD 1: PowerShell (RECOMMENDED)
Open PowerShell as Administrator on BOTH PCs:

Right-click Start → Windows PowerShell (Admin) or Terminal (Admin)

Run this command:
```
powershellNew-NetFirewallRule -DisplayName "PTT Audio App" -Direction Inbound -Protocol TCP -LocalPort 5007 -Action Allow -Profile Any
```

### 📖 What it does:
- `New-NetFirewallRule` - Creates a new firewall rule
- `-DisplayName "PTT Audio App"` - Names the rule (you'll see this name in firewall settings)
- `-Direction Inbound` - Allows **incoming** connections (other PC connecting to you)
- `-Protocol TCP` - Only affects TCP protocol (what your app uses)
- `-LocalPort 5007` - Only opens port 5007 (not all ports - secure!)
- `-Action Allow` - Allows the connection (instead of blocking)
- `-Profile Any` - Works on all network types (Domain, Private, Public)

**Expected output:**
```
Name                  : {GUID}
DisplayName           : PTT Audio App
Description           :
DisplayGroup          :
...

🔄 METHOD 2: Command Prompt (ALTERNATIVE)
Open Command Prompt as Administrator on BOTH PCs:
cmdnetsh advfirewall firewall add rule name="PTT Audio App" dir=in action=allow protocol=TCP localport=5007 profile=any
```

### 📖 What it does:
- `netsh advfirewall firewall` - Access Windows Firewall settings
- `add rule` - Create a new rule
- `name="PTT Audio App"` - Name for the rule
- `dir=in` - Direction = Inbound (incoming connections)
- `action=allow` - Allow the connection
- `protocol=TCP` - TCP protocol only
- `localport=5007` - Only port 5007
- `profile=any` - All network profiles (domain/private/public)

**Expected output:**
```
Ok.

✅ VERIFY THE RULE WAS ADDED
Check if rule exists:
powershellGet-NetFirewallRule -DisplayName "PTT Audio App"
```

**Should show:**
```
Name                  : {GUID}
DisplayName           : PTT Audio App
Enabled               : True
Direction             : Inbound
Action                : Allow

🧪 TEST AFTER ADDING RULES
Step 1: Re-enable firewall on BOTH PCs
powershellSet-NetFirewallProfile -Profile Domain,Public,Private -Enabled True
Step 2: Run your PTT app on both PCs
bashpython p2t_peer_ptt.py
```

**Should connect immediately:**
```
✅ CONNECTED - Ready for push-to-talk

🗑️ REMOVE RULE (If Needed Later)
To remove the rule later:
powershellRemove-NetFirewallRule -DisplayName "PTT Audio App"

📝 SUMMARY - RUN ON BOTH PCs:
powershell# 1. Add firewall rule
New-NetFirewallRule -DisplayName "PTT Audio App" -Direction Inbound -Protocol TCP -LocalPort 5007 -Action Allow -Profile Any

# 2. Enable firewall
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True

# 3. Test your app
python p2t_peer_ptt.py
