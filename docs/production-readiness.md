# Production Readiness Checklist

## Baseline Snapshot
- Date: 2026-05-08
- Backend import: `API_IMPORT_OK`
- Backend compile: `BACKEND_COMPILE_OK`
- Frontend lint: pass with warnings only (0 errors, 3 warnings)
- Frontend build: pass (`next build` successful)

## Current Risks
- Repository working tree is not clean (many modified/deleted files).
- Frontend still has warning-level lint issues.
- Security hardening not complete (auth, headers, rate-limiting, data retention).
- CI/CD quality gates are not yet enforced.

## Release Gates (Must Be Green)
- [ ] Backend runtime checks (`/health`, `/ready`, authenticated `/query` smoke)
- [ ] Frontend lint with no errors
- [ ] Frontend production build
- [ ] Security controls enabled and verified
- [ ] CI workflow required for merge/deploy
- [ ] Rollback runbook reviewed

## Rollout Checklist
- [ ] Staging deployment complete
- [ ] Env vars validated (no missing required keys)
- [ ] Smoke tests passed against staging
- [ ] Observability checks (logs/tracing) validated
- [ ] Production deployment approved