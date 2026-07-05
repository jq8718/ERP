from system.version import get_app_version


def erp_version(request):
    return {"erp_version": get_app_version()}
