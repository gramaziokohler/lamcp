using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using Rhino;
using Rhino.PlugIns;

[assembly: Guid("6f1c2b7e-3a94-4d1e-9b5a-8c2f4e71d0a3")]
[assembly: PlugInDescription(DescriptionType.Organization, "Gramazio Kohler Research, ETH Zürich")]
[assembly: PlugInDescription(DescriptionType.WebSite, "https://github.com/gramaziokohler/lamcp")]

namespace Lamcp
{
    /// <summary>
    /// Loads when Rhino starts and starts the LAMCP bridge (the embedded
    /// lamcp_bridge.py) once Rhino is idle. The bridge runs in Rhino's
    /// shared CPython 3 runtime, the same one Grasshopper script components use.
    /// </summary>
    public class LamcpPlugin : PlugIn
    {
        private const string AutoStartKey = "AutoStart";
        private const string ResourceName = "Lamcp.lamcp_bridge.py";
        private const string ActionLine = "_ACTION = \"toggle\"";

        public LamcpPlugin()
        {
            Instance = this;
        }

        public static LamcpPlugin Instance { get; private set; }

        public override PlugInLoadTime LoadTime => PlugInLoadTime.AtStartup;

        public bool AutoStart
        {
            get => Settings.GetBool(AutoStartKey, true);
            set => Settings.SetBool(AutoStartKey, value);
        }

        protected override LoadReturnCode OnLoad(ref string errorMessage)
        {
            if (AutoStart)
                RhinoApp.Idle += OnFirstIdle;
            return LoadReturnCode.Success;
        }

        private void OnFirstIdle(object sender, EventArgs e)
        {
            RhinoApp.Idle -= OnFirstIdle;
            RunBridge("start");
        }

        /// <summary>Run the bridge script with action "start", "stop" or "toggle".</summary>
        internal static bool RunBridge(string action)
        {
            string path;
            try
            {
                path = WriteScript(action);
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine("LAMCP: could not prepare bridge script: " + ex.Message);
                return false;
            }
            return RhinoApp.RunScript("_-ScriptEditor _Run \"" + path + "\"", false);
        }

        private static string WriteScript(string action)
        {
            string source;
            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(ResourceName))
            {
                if (stream == null)
                    throw new InvalidOperationException("embedded resource " + ResourceName + " missing");
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                    source = reader.ReadToEnd();
            }

            if (!source.Contains(ActionLine))
                throw new InvalidOperationException("action line not found in bridge script");
            source = source.Replace(ActionLine, "_ACTION = \"" + action + "\"");

            string dir = Path.Combine(Path.GetTempPath(), "lamcp");
            Directory.CreateDirectory(dir);
            string path = Path.Combine(dir, "lamcp_bridge_" + action + ".py");
            // No BOM: the `#! python3` shebang must be the first bytes of the file.
            File.WriteAllText(path, source, new UTF8Encoding(false));
            return path;
        }
    }
}
