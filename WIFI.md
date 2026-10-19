# Wi-Fi mode (Tailscale Funnel)

Off by default. Lets the tablet reach the Mac without the USB cable, over the internet, on networks that block devices from seeing each other (most campus Wi-Fi). The cable is always used when plugged in.

## What gets exposed

| | |
|---|---|
| Public URL | `https://<computer>.<tailnet>.ts.net` (Tailscale Funnel, port 443) |
| Reachable | Only the tablet button endpoints: `/snip /eval /graph /ask /solve /check /ping` |
| Auth | Secret token (random, 24 bytes), required on every request; wrong or missing token returns 404 |
| Not reachable | Dashboard, settings, quit, model API. Funnel forwards to a separate port (8798) that serves nothing else |
| Tablet TLS | BusyBox wget encrypts but does not verify certificates. Someone controlling the network could capture the token and use your model; they cannot reach files or settings |

Turn it off when you don't need it.

## Setup

1. Install Tailscale on the Mac and sign in. Mac: Tailscale from the Mac App Store or https://tailscale.com/download/mac
2. Tailscale admin console (https://login.tailscale.com/admin):
   - DNS: enable MagicDNS and HTTPS Certificates.
   - Access controls: Funnel must be allowed for this device. New tailnets allow it by default. If not, the first step 3 prints a link that adds the `funnel` attribute; open it and approve.
3. Snip2TeX dashboard, card "Wi-Fi mode": Turn on Wi-Fi mode.
   - If it shows a message with a link, open it, approve, then turn it off and on again.
   - It shows `On · https://...ts.net` when Funnel is serving.
4. Plug the tablet in over USB once. The app copies the URL and token to the tablet. The card then says "tablet has the address and token".
5. Unplug. The buttons now go over Wi-Fi (a few seconds slower). The tablet needs Wi-Fi with internet access.

A newly enabled Funnel can take up to about 10 minutes before it answers from the internet.

## Turn off

Dashboard, "Wi-Fi mode": Turn off Wi-Fi mode. Funnel stops at once. The token is removed from the tablet the next time it is plugged in. Turning it on again later reuses the token; to make a new one, delete `remote_token` from `~/.snip2tex/config.json` while the app is quit.

## Troubleshooting

| Check | Command or place |
|---|---|
| Funnel status | `/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel status` |
| Reachable from outside | `curl https://<computer>.<tailnet>.ts.net/ping?token=<remote_token from ~/.snip2tex/config.json>` returns `{"ok": true}` |
| Tablet side | `/home/root/xovi/exthome/snip2tex/server_remote` and `token` exist; failures in `/home/root/.snip2tex/log` |
| Turn Funnel off by hand | `/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --https=443 off` |

