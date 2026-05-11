# Rollback Runbook

## Trigger Conditions
- API error rate spike or repeated 5xx from `/query`
- Failed startup/readiness checks on deployment
- Critical auth or data integrity regression

## Immediate Actions
1. Pause new deploys.
2. Roll back the Render service to the previous known-good deploy.
3. Confirm backend liveness and readiness:
   - `GET /health`
   - `GET /ready`
4. Validate authenticated query flow from frontend (`/api/query`).

## Verification Checklist
- Backend logs show normal request throughput.
- Frontend loads and login succeeds.
- One end-to-end query returns successful response.
- No new secrets/config changes were introduced in rollback.

## Post-Rollback
- Capture incident timeline and root cause.
- Open follow-up tasks for prevention.
- Re-run CI checks before attempting redeploy.
