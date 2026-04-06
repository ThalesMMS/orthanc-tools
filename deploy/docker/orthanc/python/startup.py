# Demonstration script loaded by the Orthanc Python plugin.
import orthanc


def on_change(change_type, level, resource):
    if change_type == orthanc.ChangeType.ORTHANC_STARTED:
        orthanc.LogWarning("Python plugin iniciado com sucesso.")
    elif change_type == orthanc.ChangeType.NEW_INSTANCE:
        orthanc.LogWarning("Novo DICOM recebido: %s" % resource)


orthanc.RegisterOnChangeCallback(on_change)
