#!/bin/sh
# snip.sh X Y W H OUT [snip|eval|graph|ask|solve|check] [PAD] [ROT]
# X Y W H are physical framebuffer pixels; ROT is the UI rotation in degrees
# (landscape), which the Mac undoes before reading the grab. — grab framebuffer rows Y..Y+H, POST to the
# Mac, save the reply to OUT: the rendered PNG (snip), a graph PNG (graph) or
# one line of text with the evaluated result (eval, check), or text plus maybe a
# PNG (ask, solve). Started in the background by snip2tex.qmd.
# Server: the Mac over USB ($DIR/server); when that doesn't answer and Wi-Fi
# mode is set up, the Mac's Tailscale Funnel address ($DIR/server_remote) with
# the shared secret ($DIR/token).
# Writes OUT.err on failure so the QML side can stop waiting.
X=$1 Y=$2 W=$3 H=$4 OUT=$5 MODE=${6:-snip} PAD=${7:-0} ROT=${8:-0}
DIR=/home/root/xovi/exthome/snip2tex
SERVER=$(cat $DIR/server 2>/dev/null || echo http://10.11.99.13:8799)
REMOTE=$(cat $DIR/server_remote 2>/dev/null)
TOKEN=$(cat $DIR/token 2>/dev/null)
TMP=/tmp/snip2tex.raw
LOG=/home/root/.snip2tex/log

fail() { echo "$(date) $*" >> $LOG; echo "$*" > "$OUT.err"; exit 1; }

# "0xADDR,width,height,type,bytesPerLine,requiresReload"
echo '>eframebuffer-spy$getConfigString:' > /run/xovi-mb
CFG=$(cat /run/xovi-mb-out)
ADDR=$(printf "%d" "${CFG%%,*}")
REST=${CFG#*,}; FBW=${REST%%,*}
REST=${REST#*,}; FBH=${REST%%,*}
REST=${REST#*,}; REST=${REST#*,}; BPL=${REST%%,*}
[ -n "$BPL" ] || fail "bad fb config: $CFG"

# clamp to the screen
[ "$X" -lt 0 ] && { W=$((W + X)); X=0; }
[ "$Y" -lt 0 ] && { H=$((H + Y)); Y=0; }
[ $((Y + H)) -gt "$FBH" ] && H=$((FBH - Y))
[ "$H" -gt 0 ] || fail "empty rect"

dd if=/proc/$(pidof xochitl)/mem of=$TMP bs=$BPL count=$H \
    iflag=skip_bytes skip=$((ADDR + Y * BPL)) 2>/dev/null || fail "dd failed"

# busybox wget --post-file stops at the first NUL byte, so gzip + base64 it
gzip -c -1 $TMP | openssl base64 -A > $TMP.b64 || fail "encode failed"
# USB first; no cable (or the Mac's USB address changed) -> Wi-Fi via Funnel
URL=$SERVER
if [ -n "$REMOTE" ] && ! wget -q -T 3 -O /dev/null "$SERVER/ping" 2>/dev/null; then
    URL=$REMOTE
fi
# -S: response headers go to stderr (the ? button's diagram caption is one)
wget -S -T 180 -O "$OUT.part" \
    --header "Content-Type: text/plain" \
    --post-file $TMP.b64 \
    "$URL/$MODE?format=png&raw=bgra&enc=gz64&stride=$BPL&rows=$H&x=$X&w=$W&pad=$PAD&rot=$ROT&token=$TOKEN" \
    2> "$OUT.hdr" || fail "upload failed ($URL)"
rm -f $TMP $TMP.b64
# ? (a diagram) and solve (the worked steps) can answer with a PNG: put it next
# to OUT (.txt -> .png) for the popup to insert, and the caption (base64
# header) in OUT. PNG first, so it's there by the time the popup sees OUT.
if { [ "$MODE" = ask ] || [ "$MODE" = solve ]; } && dd if="$OUT.part" bs=4 count=1 2>/dev/null | grep -q PNG; then
    mv "$OUT.part" "${OUT%.txt}.png"
    sed -n 's/^ *X-Caption: *//p' "$OUT.hdr" | tr -d '\r' | openssl base64 -d -A > "$OUT.part" 2>/dev/null
    [ -s "$OUT.part" ] || echo "added" > "$OUT.part"
fi
rm -f "$OUT.hdr"

# image modes get text back when the Mac side failed: hand it to the popup
case "$OUT" in *.png)
    dd if="$OUT.part" bs=4 count=1 2>/dev/null | grep -q PNG || { mv "$OUT.part" "$OUT.err"; exit 1; } ;;
esac
mv "$OUT.part" "$OUT"
