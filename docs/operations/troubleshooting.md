# Operations: Troubleshooting

## REST does not respond

- run `./scripts/orthanc/orthanc-diagnose-rest.sh`
- validate credentials in `/etc/orthanc/credentials.json`

## Orthanc starts, but is not ready yet

- use `./scripts/orthanc/orthanc-start.sh` or `./scripts/orthanc/orthanc-restart.sh`
- these helpers wait for the healthcheck before returning success

## Sync/backfill fails on one study

- run the workflow again; the state is resumable
- check logs in the configured state directory
