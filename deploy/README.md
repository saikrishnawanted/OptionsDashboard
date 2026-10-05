# Ubuntu deployment by IP

The deployed dashboard uses HTTPS, Nginx browser login, and a single loopback-only Python worker. Broker authentication is separate and still requires today's TOTP. The service starts in dry mode and never arms live trading after a restart.

On a fresh Ubuntu server, install `nginx apache2-utils python3-venv git certbot`, clone this repository to `/opt/OptionsDashboard`, then run:

```bash
sudo bash /opt/OptionsDashboard/deploy/install-ubuntu.sh YOUR_SERVER_IP
```

TCP 80 must be reachable for certificate issuance and renewal, and 443 for the dashboard. Port 8765 stays private. The installer is for a fresh server and refuses to replace existing deployment credentials. It uses Python 3.12 and the Linux requirements file.

Open `https://YOUR_SERVER_IP`. The initial browser username/password is stored root-only at `/etc/optionsdashboard/initial-access.json`. The service's proxy secret is not a browser login and must never be shared. Nginx authenticates HTTP and WebSocket requests before forwarding; the app additionally checks the secret, loopback peer, configured host and exact HTTPS origin. Existing CSRF and live-order confirmations still apply.

Linux broker credentials use Fernet encryption. Data and its owner-only key are under `/var/lib/optionsdashboard`; the browser password hash and service environment are under `/etc/optionsdashboard`. Windows DPAPI credentials cannot be copied to Linux: enter broker credentials anew. Keep backups of the Linux database, encrypted credentials and vault key private; never commit them. Copy a trading database only after the original process is stopped and broker orders are reconciled, with only one scheduler running against the account.

The daily certificate timer runs renewal with a Nginx reload hook. IP certificates last six days, so keep port 80 available and monitor renewal. See [Let's Encrypt's IP certificate instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot) and [Uvicorn deployment](https://www.uvicorn.org/deployment/).

Useful checks:

```bash
sudo systemctl status optionsdashboard nginx
sudo journalctl -u optionsdashboard -n 50 --no-pager
sudo systemctl list-timers optionsdashboard-cert-renew.timer
```

Update source and dependencies only after positions and broker orders are reconciled, then restart `optionsdashboard`. Restart pauses recovered strategies and disarms live entry; reconnect and review before resuming. Do not use multiple workers, reload mode or duplicate services for trading.
