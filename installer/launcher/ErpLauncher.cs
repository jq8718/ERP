using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

internal static class ErpLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string exeName = Path.GetFileNameWithoutExtension(Assembly.GetExecutingAssembly().Location).ToLowerInvariant();
        string scriptName;
        if (exeName.Contains("uninstall"))
        {
            scriptName = "erp-uninstall-launcher.ps1";
        }
        else if (exeName.Contains("update"))
        {
            scriptName = "erp-update-launcher.ps1";
        }
        else
        {
            scriptName = "erp-setup-launcher.ps1";
        }
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        string scriptPath = Path.Combine(exeDir, "installer", scriptName);
        string logPath = CreateLauncherLog(exeDir, exeName, scriptPath);
        AppendLauncherLog(logPath, "Launcher started.");

        if (!File.Exists(scriptPath))
        {
            AppendLauncherLog(logPath, "Installer script missing.");
            ShowError(
                "Installer script was not found:\n\n" + scriptPath + "\n\n" +
                "Keep ERP-Setup.exe / ERP-Update.exe / ERP-Uninstall.exe with installer and manage.py in the ERP package root.\n\n" +
                "Launcher log:\n" + logPath
            );
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
                AppendLauncherLog(logPath, "Process.Start returned null.");
                ShowError("PowerShell did not start. Right-click the ERP launcher exe and choose Run as administrator.\n\nLauncher log:\n" + logPath);
                return 1;
            }
            AppendLauncherLog(logPath, "PowerShell started. PID: " + process.Id);
            return 0;
        }
        catch (Exception ex)
        {
            AppendLauncherLog(logPath, "Failed to start PowerShell: " + ex);
            ShowError(
                "ERP setup could not start:\n\n" + ex.Message + "\n\n" +
                "Right-click the ERP launcher exe and choose Run as administrator. If Windows asks for permission, click Yes.\n\n" +
                "Launcher log:\n" + logPath
            );
            return 1;
        }
    }

    private static void ShowError(string message)
    {
        MessageBox.Show(message, "ERP Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private static string CreateLauncherLog(string exeDir, string exeName, string scriptPath)
    {
        string logDir = Path.Combine(exeDir, "installer", "logs");
        try
        {
            Directory.CreateDirectory(logDir);
        }
        catch
        {
            logDir = Path.GetTempPath();
        }

        string stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss");
        string logPath = Path.Combine(logDir, "erp-launcher-" + exeName + "-" + stamp + ".log");
        AppendLauncherLog(logPath, "Executable directory: " + exeDir);
        AppendLauncherLog(logPath, "Script path: " + scriptPath);
        AppendLauncherLog(logPath, "OS user: " + Environment.UserName);
        return logPath;
    }

    private static void AppendLauncherLog(string logPath, string message)
    {
        try
        {
            File.AppendAllText(logPath, DateTime.Now.ToString("s") + " " + message + Environment.NewLine);
        }
        catch
        {
        }
    }
}
