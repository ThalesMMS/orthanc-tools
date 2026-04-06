# Docker Stack

This stack provides a quick local Orthanc environment with Orthanc Explorer 2, DICOMweb, OHIF, and the Python plugin enabled.

## Canonical Entry Point

```bash
docker compose -f deploy/docker/compose.yaml up -d
docker compose -f deploy/docker/compose.yaml ps
docker compose -f deploy/docker/compose.yaml logs -f orthanc
```

## Structure

- `compose.yaml`: canonical Docker Compose file
- `orthanc/config/orthanc.json`: main Orthanc configuration
- `orthanc/python/startup.py`: Python plugin bootstrap
- `orthanc/plugins/ohif/`: OHIF plugin files

## Security Posture of the Shipped Example

The current `orthanc.json` is intentionally permissive for fast local testing.

It currently includes:

- `RemoteAccessAllowed: true`
- `AuthenticationEnabled: false`
- `RegisteredUsers: {"admin":"admin"}`
- `DicomAlwaysAllowEcho: true`
- `DicomAlwaysAllowFind: true`
- `DicomAlwaysAllowMove: true`
- `DicomAlwaysAllowStore: true`

This is not an appropriate long-term or exposed deployment baseline. Before using this stack beyond local testing, change the authentication settings, restrict remote access, and tighten DICOM permissions.

## Customization

- update HTTP credentials in `orthanc/config/orthanc.json`
- update AE Title and remote modalities in `DicomAet` and `DicomModalities`
- adjust host ports in `compose.yaml`

## Related Docs

- quickstart: [`../../docs/quickstart/docker.md`](../../docs/quickstart/docker.md)
- workflows: [`../../docs/workflows`](../../docs/workflows)
