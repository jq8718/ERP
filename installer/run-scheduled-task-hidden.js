var shell = WScript.CreateObject("WScript.Shell");

if (WScript.Arguments.length < 3) {
    WScript.Quit(2);
}

function quote(value) {
    return '"' + String(value).replace(/"/g, '\\"') + '"';
}

var runner = WScript.Arguments(0);
var installDir = WScript.Arguments(1);
var commandName = WScript.Arguments(2);
var command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " + quote(runner) + " -InstallDir " + quote(installDir) + " -CommandName " + quote(commandName);
var exitCode = shell.Run(command, 0, true);
WScript.Quit(exitCode);
