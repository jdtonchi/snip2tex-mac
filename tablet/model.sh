#!/bin/sh
# model.sh OUT [set ID | thinking 0|1 | wifi on|off]
# The toolbar ✦ menu (snip2tex-toolbar.qmd): asks the Mac which models it can
# use (or switches model / thinking first) and saves its JSON reply to OUT,
# plus which way it got there ("route": usb | wifi), and a copy to
# $DIR/model.json so the menu opens with the last known state.
# "wifi on" makes snip.sh and this script try Wi-Fi mode (Tailscale Funnel)
# before USB ($DIR/prefer_wifi); if the tablet doesn't have the Funnel address
# yet, it asks the Mac over USB to switch Wi-Fi mode on and waits for the Mac
# to push the address and token. Writes OUT.err on failure.
OUT=$1 KEY=$2 VAL=$3
DIR=/home/root/xovi/exthome/snip2tex
SERVER=$(cat $DIR/server 2>/dev/null || echo http://10.11.99.13:8799)
REMOTE=$(cat $DIR/server_remote 2>/dev/null)
TOKEN=$(cat $DIR/token 2>/dev/null)
NOTE=""

# replies from earlier menus
find /home/root/.snip2tex -name 'model-*' -mmin +2 -exec rm -f {} \; 2>/dev/null

fail() { rm -f "$OUT.part"; echo "$*" > "$OUT.err"; exit 1; }

if [ "$KEY" = wifi ] && [ "$VAL" = off ]; then
    rm -f $DIR/prefer_wifi
elif [ "$KEY" = wifi ]; then
    touch $DIR/prefer_wifi
    if [ -z "$REMOTE" ]; then
        # not set up on the tablet yet: ask the Mac (USB) to turn Wi-Fi mode on
        wget -q -T 30 -O "$OUT.part" --post-data "x" "$SERVER/model?wifi=1" 2>/dev/null \
            || fail "Wi-Fi mode needs the USB cable once, to set it up"
        if grep -q '"wifi_enabled": *false' "$OUT.part"; then
            ERR=$(sed -n 's/.*"wifi_error": *"\([^"]*\)".*/\1/p' "$OUT.part")
            fail "Mac couldn't start Wi-Fi mode: ${ERR:-is Tailscale running?}"
        fi
        # the Mac's watcher pushes the address and token within ~8 s
        i=0
        while [ ! -s $DIR/server_remote ] && [ $i -lt 25 ]; do sleep 1; i=$((i + 1)); done
        REMOTE=$(cat $DIR/server_remote 2>/dev/null)
        TOKEN=$(cat $DIR/token 2>/dev/null)
        [ -n "$REMOTE" ] || fail "Wi-Fi mode is on, but the Mac hasn't sent the address yet; try again"
    fi
fi

ROUTE=usb URL=$SERVER
if [ -n "$REMOTE" ] && [ -f $DIR/prefer_wifi ]; then
    if wget -q -T 8 -O /dev/null "$REMOTE/ping?token=$TOKEN" 2>/dev/null; then
        ROUTE=wifi URL=$REMOTE
    else
        NOTE="Wi-Fi didn't answer, using USB"
    fi
elif [ -n "$REMOTE" ] && ! wget -q -T 3 -O /dev/null "$SERVER/ping" 2>/dev/null; then
    ROUTE=wifi URL=$REMOTE
fi
[ -f $DIR/prefer_wifi ] && PREFER=true || PREFER=false
[ -n "$REMOTE" ] && READY=true || READY=false

Q="token=$TOKEN"
case "$KEY" in set|thinking) Q="$Q&$KEY=$VAL" ;; esac
if ! wget -q -T 20 -O "$OUT.part" --post-data "x" "$URL/model?$Q" 2>/dev/null; then
    [ "$ROUTE" = wifi ] && fail "Mac not reachable over Wi-Fi (is it awake, with Tailscale on?)"
    fail "Mac not reachable over USB${REMOTE:+ or Wi-Fi}"
fi
# add how we got here to the Mac's JSON
sed -i "s/^{/{\"route\": \"$ROUTE\", \"prefer_wifi\": $PREFER, \"wifi_ready\": $READY, \"note\": \"$NOTE\", /" "$OUT.part"
cp "$OUT.part" "$DIR/model.json"
mv "$OUT.part" "$OUT"
