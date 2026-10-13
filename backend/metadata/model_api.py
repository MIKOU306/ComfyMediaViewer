"""Strict JSON request dispatch for the model service; no client receipts/hashes."""


def dispatch_model_write(service, action, request):
    if not isinstance(request, dict):
        raise ValueError("Request must be an object")
    fields = {
        "roots/register": {"path", "name"},
        "verification/start": {"rootUuid", "relativePath"},
        "verification/cancel": {"jobUuid"},
        "files/register": {"rootUuid", "relativePath", "jobUuid", "modelType"},
        "references/confirm": {"mediaUuid", "revisionUuid", "referenceKey", "modelUuid",
                               "locationUuid", "rootUuid", "relativePath", "freshJobUuid",
                               "expectedOverrideVersion"},
        "references/revoke": {"mediaUuid", "revisionUuid", "referenceKey", "expectedOverrideVersion"},
    }
    required = fields.get(action)
    if required is None:
        raise ValueError("Unknown model action")
    if set(request) != required or any(not isinstance(request[key], str) or not request[key].strip()
                                       for key in required - {"expectedOverrideVersion"}):
        raise ValueError("Invalid model request fields")
    if "expectedOverrideVersion" in required:
        version = request["expectedOverrideVersion"]
        if type(version) is not int or version < 0:
            raise ValueError("Expected override version must be a non-negative integer")
        if action == "references/revoke":
            return service.revoke_reference(request["mediaUuid"], request["revisionUuid"],
                                             request["referenceKey"], version)
        return service.confirm_reference(request["mediaUuid"], request["revisionUuid"],
            request["referenceKey"], request["modelUuid"], expected_override_version=version,
            root_uuid=request["rootUuid"], relative_path=request["relativePath"],
            fresh_job_uuid=request["freshJobUuid"], location_uuid=request["locationUuid"])
    if action == "roots/register":
        return {"rootUuid": service.register_root(request["path"], request["name"])}
    if action == "verification/start":
        return service.start_verification(request["rootUuid"], request["relativePath"])
    if action == "verification/cancel":
        return service.cancel_verification(request["jobUuid"])
    if request["modelType"] not in ("checkpoint", "lora", "embedding", "vae", "controlnet", "other"):
        raise ValueError("Unknown model type")
    return service.register_verified_file(request["rootUuid"], request["relativePath"],
                                          request["jobUuid"], request["modelType"])
