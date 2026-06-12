using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;

internal static class ErpLauncher
{
    private static int Main(string[] args)
    {
        string exeName = Path.GetFileNameWithoutExtension(Assembly.GetExecutingAssembly().Location).ToLowerInvariant();
        string scriptName = exeName.Contains("uninstall") ? "erp-uninstall-launcher.ps1" : "erp-setup-launcher.ps1";
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        string scriptPath = Path.Combine(exeDir, "installer", scriptName);

        if (!File.Exists(scriptPath))
        {
            Console.Error.WriteLine("Cannot find installer script: " + scriptPath);
            Console.Error.WriteLine("Please keep ERP-Setup.exe / ERP-Uninstall.exe in the ERP project root directory.");
            Console.ReadLine();
            return 1;
        }

        string arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + scriptPath + "\"";
        ProcessStartInfo startInfo = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = arguments,
            WorkingDirectory = exeDir,
            UseShellExecute = true,
            Verb = "runas"
        };

        try
        {
            Process process = Process.Start(startInfo);
            if (process == null)
            {
                return 1;
            }
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            Console.ReadLine();
            return 1;
        }
    }
}
