"""Run with the Python matching python3-uno; private stdin protocol, never HTTP."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


def property_value(name, value):
    import uno

    prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    prop.Name, prop.Value = name, value
    return prop


def main():
    import uno

    profile, soffice = Path(sys.argv[1]), sys.argv[2]
    pipe = "spreadsheet_" + uuid4().hex
    office = subprocess.Popen(
        [
            soffice,
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            f"--accept=pipe,name={pipe};urp;StarOffice.ServiceManager",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = uno.getComponentContext()
    resolver = context.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", context)
    deadline = time.monotonic() + 45
    while True:
        try:
            context = resolver.resolve(f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext")
            break
        except Exception:
            if office.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("LibreOffice startup failed") from None
            time.sleep(0.1)
    desktop = context.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
    version = subprocess.check_output([soffice, "--version"], timeout=15, text=True).strip()
    print(json.dumps({"ok": True, "version": version}), flush=True)
    try:
        for line in sys.stdin:
            doc = None
            try:
                path = Path(json.loads(line)["path"]).resolve()
                options = tuple(
                    property_value(k, v)
                    for k, v in (
                        ("Hidden", True),
                        ("ReadOnly", False),
                        ("UpdateDocMode", uno.getConstantByName("com.sun.star.document.UpdateDocMode.NO_UPDATE")),
                        (
                            "MacroExecutionMode",
                            uno.getConstantByName("com.sun.star.document.MacroExecMode.NEVER_EXECUTE"),
                        ),
                    )
                )
                doc = desktop.loadComponentFromURL(path.as_uri(), "_blank", 0, options)
                if doc is None or not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
                    raise RuntimeError("Not a spreadsheet")
                doc.calculateAll()
                # Replace only the staging file, never the immutable source/oracle.
                doc.storeAsURL(
                    path.as_uri(),
                    tuple(
                        property_value(k, v)
                        for k, v in (
                            ("FilterName", "Calc MS Excel 2007 XML"),
                            ("Overwrite", True),
                        )
                    ),
                )
                print(json.dumps({"ok": True}), flush=True)
            except Exception:
                print(json.dumps({"ok": False}), flush=True)
            finally:
                if doc is not None:
                    doc.close(True)
    finally:
        office.terminate()
        office.wait(timeout=10)


if __name__ == "__main__":
    # Library exceptions may mention local file paths. The service receives only
    # the bounded protocol above; stderr is kept private by its supervisor.
    os.umask(0o077)
    main()
