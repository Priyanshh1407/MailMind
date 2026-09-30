#!/usr/bin/env bash
# API contract checks against a SANDBOX MailMind API (never the live one).
# Usage (Git Bash, project root), after starting and seeding the sandbox:
#   SANDBOX_DB="<sandbox dir>/email_logs.db" bash scripts/sandbox_api_checks.sh [http://127.0.0.1:18000]
# Ends by DELETING the sandbox account's data, so it refuses port 8000.
cd "$(dirname "$0")/.." || exit 1
API=${1:-http://127.0.0.1:18000}
case "$API" in *:8000|*:8000/) echo "Refusing: $API is the live API. Use the sandbox port."; exit 2;; esac
[ -f "${SANDBOX_DB:-}" ] || { echo "Set SANDBOX_DB to the sandbox email_logs.db"; exit 2; }
source scripts/mailmind_api.sh "$API"
mm_login || { echo "Cannot open a session at $API"; exit 1; }

# PASS/FAIL on a computed value: mm_assert "label" "$actual" "$expected"
mm_assert() {
  if [ "$2" = "$3" ]; then MM_PASS=$((MM_PASS+1)); echo "PASS  $1 ($2)"
  else MM_FAIL=$((MM_FAIL+1)); echo "FAIL  $1 (got '$2', want '$3')"; fi
}

echo "== A1 security boundary"
mm_check 401 "no session cookie is refused"                       "$MM_API/status"
mm_check 403 "session from a foreign website origin is refused"   -H "Origin: https://evil.example" -X POST "$MM_API/session"
mm_check 400 "DNS-rebinding style Host header is refused"         -H "Host: evil.example" "$MM_API/"
mm_check 403 "mutation without an Origin header is refused"       -b "$MM_JAR" -X POST "$MM_API/inbox/sync"
mm_check 401 "mutation with a wrong CSRF token is refused"        -b "$MM_JAR" -H "Origin: $MM_ORIGIN" -H "X-CSRF-Token: wrong" -X POST "$MM_API/inbox/sync"
mm_check 401 "forged session cookie is refused"                   -b "mailmind_session=forged" "$MM_API/status"
mm_check_get 200 "valid session reads status"                     /status

echo "== A2 input limits"
mm_check_get 422 "page size above 200 is rejected"                "/emails?limit=201"
mm_check_get 422 "negative offset is rejected"                    "/emails?offset=-1"
mm_check_get 422 "unknown category is rejected"                   "/emails?category=URGENT"
mm_check_get 422 "search longer than 200 characters is rejected"  "/emails?search=$(printf 'a%.0s' $(seq 1 201))"
mm_check_send 422 "feedback with an invented label is rejected"   POST /feedback '{"email_id":"x","label":"URGENT"}'
mm_check_send 404 "feedback on an email you do not own is 404"    POST /feedback '{"email_id":"not-mine","label":"SPAM"}'
mm_check_send 422 "backfill limit above 100 is rejected"          POST /intelligence/backfill '{"limit":101}'
mm_check_send 422 "empty manual prediction is rejected"           POST /predict '{"subject":"  ","body":""}'
mm_check_send 422 "action status outside the enum is rejected"    PATCH /actions/1 '{"status":"deleted","expected_revision":0}'

echo "== A3 reads"
mm_check_get 200 "email list"                                     "/emails?limit=20"
mm_check_get 200 "telemetry"                                      /telemetry
mm_check_get 200 "action list"                                    /actions
mm_check_get 200 "action summary"                                 /actions/summary
mm_check_get 200 "token analytics (day)"                          "/analytics/tokens?window=day"
mm_check_get 422 "token analytics window outside the enum"        "/analytics/tokens?window=year"
mm_check_get 200 "intelligence diagnostics"                       /diagnostics/intelligence
mm_assert "all four lanes are present" \
  "$(mm_get '/emails?limit=40' | mm_field "','.join(sorted({e['effective_category'] or 'REVIEW' for e in d['emails']}))")" \
  "IMPORTANT,REVIEW,SPAM,UPDATES"

echo "== A4 feedback lifecycle"
EID=$(mm_get '/emails?category=UPDATES&limit=1' | mm_field "d['emails'][0]['id']")
mm_check_send 202 "a correction is saved"                         POST /feedback "{\"email_id\":\"$EID\",\"label\":\"IMPORTANT\",\"expected_revision_id\":0}"
mm_check_send 409 "a stale revision is a conflict, not an overwrite" POST /feedback "{\"email_id\":\"$EID\",\"label\":\"SPAM\",\"expected_revision_id\":0}"
mm_assert "the correction wins immediately" \
  "$(mm_get "/emails?email_id=$EID" | mm_field "d['emails'][0]['effective_category']")" "IMPORTANT"
mm_check_send 202 "undo restores the model label"                 DELETE "/feedback/$EID?expected_revision_id=1"
mm_check_send 409 "undo twice is refused"                         DELETE "/feedback/$EID?expected_revision_id=2"
mm_assert "history keeps both revisions" \
  "$(mm_get "/emails/$EID/history" | mm_field "len(d['feedback'])")" "2"

echo "== A5 action lifecycle"
AID=$(mm_get '/actions' | mm_field "d['actions'][0]['action_id']")
mm_check_send 200 "complete an action"                            PATCH "/actions/$AID" '{"status":"completed","expected_revision":0}'
mm_check_send 409 "a stale action revision is a conflict"         PATCH "/actions/$AID" '{"status":"dismissed","expected_revision":0}'
mm_check_send 200 "reopen with the current revision"              PATCH "/actions/$AID" '{"status":"open","expected_revision":1}'
mm_check_send 422 "snoozing into the past is rejected"            POST "/actions/$AID/snooze" '{"snoozed_until":"2020-01-01T00:00:00Z","expected_revision":2}'
mm_check_send 404 "an action that does not exist is 404"          PATCH /actions/999999 '{"status":"completed","expected_revision":0}'

echo "== A6 intake in local-only mode"
mm_check_send 403 "Gmail sync is disabled in local-only mode"     POST /inbox/sync
mm_check_send 202 "backfill queues saved emails"                  POST /intelligence/backfill '{"limit":5}'

echo "== A7 search edge cases"
mm_check_get 200 "1-character search"                            "/emails?search=a"
mm_check_get 200 "SQL wildcard characters are literal"            "/emails?search=100%25_%5C"
mm_assert "'%' matches nothing (not a wildcard)" "$(mm_get '/emails?search=%25' | mm_field "d['total']")" "0"

echo "== A8 local model"
for _ in $(seq 1 60); do [ "$(mm_get / | mm_field "d['model_loaded']")" = "True" ] && break; sleep 2; done
mm_assert "manual prediction is classified by the local model" \
  "$(mm_send POST /predict '{"subject":"Contract signature needed today","body":"Please sign the revised contract before 5 pm today."}' | mm_field "d['status'] + '/' + d['source']")" \
  "success/local"

echo "== A9 two conflicting corrections at the same instant"
EID=$(mm_get '/emails?category=SPAM&limit=1' | mm_field "d['emails'][0]['id']")
REV=$(mm_get "/emails/$EID/history" | mm_field "max([r['revision_id'] for r in d['feedback']] or [0])")
CODES=$(for label in IMPORTANT UPDATES; do
  curl -s -o /dev/null -w "%{http_code}\n" -b "$MM_JAR" -H "Origin: $MM_ORIGIN" -H "X-CSRF-Token: $MM_CSRF" \
    -H 'Content-Type: application/json' -X POST \
    -d "{\"email_id\":\"$EID\",\"label\":\"$label\",\"expected_revision_id\":$REV}" "$MM_API/feedback" &
done | tr -d '\r' | sort | xargs; wait)
# Windows curl may emit \r after each code; xargs also normalises whitespace.
mm_assert "exactly one wins, the other gets a conflict" "$CODES" "202 409"

echo "== A10 re-analysis"
RID=$(mm_get '/emails?category=IMPORTANT&limit=1' | mm_field "d['emails'][0]['id']")
REVAT=$(mm_get "/emails?email_id=$RID" | mm_field "(d['emails'][0].get('analysis') or {}).get('updated_at')")
BODY=$([ "$REVAT" = "None" ] && echo '{}' || echo "{\"expected_analysis_updated_at\":\"$REVAT\"}")
FIRST=$(mm_send POST "/emails/$RID/reanalyze" "$BODY")
mm_assert "re-analysis succeeds" "$(echo "$FIRST" | tail -1)" "[HTTP 200]"
NEWREV=$(echo "$FIRST" | mm_field "d['analysis_revision']")
mm_check_send 409 "re-analysis with a stale revision is a conflict" POST "/emails/$RID/reanalyze" '{}'
mm_check_send 429 "re-analysis again within the cooldown is rate limited" POST "/emails/$RID/reanalyze" "{\"expected_analysis_updated_at\":\"$NEWREV\"}"

echo "== A11 delete account data (sandbox only)"
mm_check_send 200 "delete this account's data"                    DELETE /account-data
mm_assert "no saved email remains for the account" "$("$MM_PY" -c "
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
print(connection.execute(\"SELECT COUNT(*) FROM email_logs WHERE account_id='sandbox@example.test'\").fetchone()[0])" "$SANDBOX_DB")" "0"

mm_summary
[ "$MM_FAIL" -eq 0 ]
