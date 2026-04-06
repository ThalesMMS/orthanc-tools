## Summary

- What changed?
- Why was it needed?

## Validation

- [ ] `python3 -m unittest discover -s tests -p 'test_*.py'`
- [ ] `python3 -m compileall src scripts orthanc_tools`
- [ ] shell syntax checks for `scripts/` and `deploy/`
- [ ] `docker compose -f deploy/docker/compose.yaml config`

## Docs Impact

- [ ] README updated
- [ ] deployment or workflow docs updated
- [ ] no docs changes needed

## Security Impact

- [ ] no security-relevant change
- [ ] changes security posture or exposed defaults
- [ ] changes examples or operator guidance
