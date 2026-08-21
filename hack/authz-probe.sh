#!/usr/bin/env bash
# Measure the three authorization fixes against the running stand.
#
# Needs a bearer token from a live session, which is why this is a script you
# run rather than something the assistant does: the stand authenticates through
# dex, and getting a token means typing a password.
#
#   1. open the UI, sign in, open devtools → Application → Local Storage
#   2. copy the access token
#   3. TOKEN=... hack/authz-probe.sh [base-url]
#
# Run it TWICE: once against the backend that predates the fixes, once after
# the rollout. The pair is the measurement; either half alone is a screenshot.
#
# Two of the three probes need a NON-admin session (kv-member): a platform
# admin passes those checks by design, so an admin token cannot show the
# difference. The schedule probe does not care who you are — that guard is not
# role-based — so an admin token measures it exactly.
set -uo pipefail

base="${1:-https://kubevirt-ui.lab.beardlabs.cc}"
: "${TOKEN:?set TOKEN to a session bearer token}"
api="$base/api/v1"
auth=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")

say() { printf '\n=== %s\n' "$1"; }
code() { curl -sS -o /tmp/authz-body -w '%{http_code}' "$@"; }
body() { head -c 300 /tmp/authz-body; echo; }

say "who am I"
curl -sS "${auth[@]}" "$api/auth/me" | head -c 300; echo

# --- MF-1: granting project access -----------------------------------------
# Inert on purpose: the project does not exist, so a caller who gets past the
# door lands in the handler and is told 404. Refused at the door is 403.
say "MF-1  POST /projects/no-such-project/access   (404 = got in, 403 = refused)"
code "${auth[@]}" -X POST "$api/projects/no-such-project/access" \
	-d '{"type":"user","name":"probe","role":"admin"}'
echo; body

# --- MF-3: a schedule that targets another namespace ------------------------
# This one creates something if it is allowed to, so it is cleaned up below.
say "MF-3  POST /schedules  (201 = the hole, 403 = closed)"
code "${auth[@]}" -X POST "$api/schedules?namespace=poc-transit-dev" \
	-d '{"display_name":"authz probe","action":"stop","schedule":"0 5 31 2 *",
	     "vm_name":"probe-no-such-vm","vm_namespace":"tenant-uat-t2"}'
echo; body
name="$(python3 -c 'import json,sys
try: print(json.load(open("/tmp/authz-body")).get("name",""))
except Exception: print("")')"
if [ -n "$name" ]; then
	echo "created $name — deleting it again"
	code "${auth[@]}" -X DELETE "$api/schedules/$name?namespace=poc-transit-dev"; echo
fi

# --- MF-2: backing up somebody else's namespace -----------------------------
say "MF-2  POST /velero/backups over a foreign namespace  (2xx = the hole, 403 = closed)"
code "${auth[@]}" -X POST "$api/velero/backups" \
	-d '{"name":"authz-probe","included_namespaces":["kube-system"],
	     "snapshot_volumes":false,"ttl":"1h"}'
echo; body
say "MF-2  and the whole cluster, which is what an empty request means to Velero"
code "${auth[@]}" -X POST "$api/velero/backups" \
	-d '{"name":"authz-probe-all","snapshot_volumes":false,"ttl":"1h"}'
echo; body
echo
echo "if either backup was created, delete it:"
echo "  curl -X DELETE $api/velero/backups/authz-probe     -H 'Authorization: Bearer …'"
echo "  curl -X DELETE $api/velero/backups/authz-probe-all -H 'Authorization: Bearer …'"
