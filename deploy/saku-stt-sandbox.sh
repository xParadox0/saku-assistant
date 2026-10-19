#!/bin/sh
# Start the Saku speech bridge inside a bubblewrap sandbox.
#
# The bridge decodes audio that strangers can send to the bot, so the decoder gets as little as possible:
# - no network (all namespaces unshared), no capabilities, a new session;
# - an empty /home: only the virtualenv, the model and the saku package are visible, all read-only;
# - the only writable path is the socket directory; /tmp is a private tmpfs.
# Secrets in ~/.config, ~/.ssh, ~/.hermes, statement samples and the deploy directory are not mounted.
set -eu

HOME_DIR=${SAKU_STT_HOME:-$HOME}
VENV="$HOME_DIR/.local/share/saku-stt/venv"
MODEL="$HOME_DIR/.local/share/saku-stt/models/small"
RUN_DIR="$HOME_DIR/.local/run/saku-stt"
SRC="$(cd "$(dirname "$0")/.." && pwd)/saku"

mkdir -p "$RUN_DIR"
chmod 711 "$RUN_DIR"
for need in "$VENV/bin/python" "$MODEL/model.bin" "$SRC/stt_bridge.py"; do
  [ -e "$need" ] || { echo "missing: $need" >&2; exit 1; }
done

exec bwrap --die-with-parent --new-session --unshare-all --cap-drop ALL \
  --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/bin /bin \
  --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind /etc/localtime /etc/localtime \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /home \
  --ro-bind "$VENV" /opt/venv \
  --ro-bind "$MODEL" /opt/model \
  --ro-bind "$SRC" /opt/app/saku \
  --bind "$RUN_DIR" /run/saku-stt \
  --clearenv --setenv PATH /usr/bin --setenv HOME /tmp --setenv LANG C.UTF-8 \
  --setenv PYTHONPATH /opt/app --setenv PYTHONDONTWRITEBYTECODE 1 --setenv HF_HUB_OFFLINE 1 \
  --chdir /tmp \
  /opt/venv/bin/python -m saku.stt_bridge --model /opt/model --threads 2 --socket /run/saku-stt/stt.sock
