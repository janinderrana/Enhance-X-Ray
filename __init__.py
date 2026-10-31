from calibre.customize import InterfaceActionBase

VERSION = (1, 2, 16)


class EnhanceXRayBase(InterfaceActionBase):
    name = "Enhance X-Ray"
    description = (
        "Rewrite WordDumb X-Ray entity names and descriptions with the Claude API "
        "or a local model (oMLX), chosen in a dialog. Skips silently if the model is "
        "unreachable, leaving the X-Ray untouched."
    )
    supported_platforms = ["windows", "osx", "linux"]
    author = "local"
    version = VERSION
    minimum_calibre_version = (5, 0, 0)
    actual_plugin = "calibre_plugins.claude_xray.ui:EnhanceXRayAction"

    def is_customizable(self):
        return False
