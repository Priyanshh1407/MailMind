# MailMind API helpers for Git Bash. Usage (from the project root):
#   source scripts/mailmind_api.sh                  # live API on 127.0.0.1:8000
#   source scripts/mailmind_api.sh http://127.0.0.1:18000   # sandbox API
# Then: mm_login, mm_get /status, mm_send POST /feedback '{...}', mm_check ...
# Needs only curl and the project's Python (no jq). Opening a session here does
# not sign the dashboard out; each browser/shell keeps its own session.

MM_API=${1:-${MM_API:-http://127.0.0.1:8000}}
MM_ORIGIN=${MM_ORIGIN:-http://127.0.0.1:5173}
MM_PY=${MM_PY:-./venv/Scripts/python.exe}
MM_JAR=${MM_JAR:-$(mktemp -t mailmind-cookies.XXXXXX)}
MM_PASS=0
MM_FAIL=0
export MM_API MM_ORIGIN MM_PY MM_JAR

# Open a local session (as the dashboard does) and keep its CSRF token.
mm_login() {
  MM_CSRF=$(curl -s -c "$MM_JAR" -b "$MM_JAR" -H "Origin: $MM_ORIGIN" -X POST "$MM_API/session" \
    | "$MM_PY" -c 'import sys,json; print(json.load(sys.stdin)["csrf_token"])') || return 1
  export MM_CSRF
  echo "session opened at $MM_API"
}

# GET with the session; prints the body and the HTTP status.
mm_get() { curl -s -b "$MM_JAR" -w '\n[HTTP %{http_code}]\n' "$MM_API$1"; }

# METHOD PATH [JSON]: a mutation with session, Origin and CSRF, like the dashboard.
mm_send() {
  curl -s -b "$MM_JAR" -H "Origin: $MM_ORIGIN" -H "X-CSRF-Token: $MM_CSRF" \
    -H 'Content-Type: application/json' -X "$1" -d "${3:-{\}}" \
    -w '\n[HTTP %{http_code}]\n' "$MM_API$2"
}

# Print one JSON field from stdin: mm_get /status | mm_field "d['connected']"
mm_field() {
  sed '$d' | "$MM_PY" -c 'import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"
}

# EXPECTED "label" curl-args...: run curl and report PASS/FAIL on the status code.
mm_check() {
  local expected=$1 label=$2 code
  shift 2
  code=$(curl -s -o /dev/null -w '%{http_code}' "$@")
  if [ "$code" = "$expected" ]; then MM_PASS=$((MM_PASS+1)); echo "PASS  [$code] $label"
  else MM_FAIL=$((MM_FAIL+1)); echo "FAIL  [got $code, want $expected] $label"; fi
}

# Shorthands for mm_check with the session (and CSRF for mutations).
mm_check_get() { mm_check "$1" "$2" -b "$MM_JAR" "$MM_API$3"; }
mm_check_send() {
  mm_check "$1" "$2" -b "$MM_JAR" -H "Origin: $MM_ORIGIN" -H "X-CSRF-Token: $MM_CSRF" \
    -H 'Content-Type: application/json' -X "$3" -d "${5:-{\}}" "$MM_API$4"
}

mm_summary() { echo "---- $MM_PASS passed, $MM_FAIL failed"; }
