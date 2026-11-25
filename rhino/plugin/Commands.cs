using Rhino;
using Rhino.Commands;

namespace Lamcp
{
    // ScriptRunner style is required to call RhinoApp.RunScript from a command.

    [CommandStyle(Style.ScriptRunner)]
    public class LamcpStartCommand : Command
    {
        public override string EnglishName => "LamcpStart";

        protected override Result RunCommand(RhinoDoc doc, RunMode mode) =>
            LamcpPlugin.RunBridge("start") ? Result.Success : Result.Failure;
    }

    [CommandStyle(Style.ScriptRunner)]
    public class LamcpStopCommand : Command
    {
        public override string EnglishName => "LamcpStop";

        protected override Result RunCommand(RhinoDoc doc, RunMode mode) =>
            LamcpPlugin.RunBridge("stop") ? Result.Success : Result.Failure;
    }

    [CommandStyle(Style.ScriptRunner)]
    public class LamcpToggleCommand : Command
    {
        public override string EnglishName => "Lamcp";

        protected override Result RunCommand(RhinoDoc doc, RunMode mode) =>
            LamcpPlugin.RunBridge("toggle") ? Result.Success : Result.Failure;
    }

    public class LamcpAutoStartCommand : Command
    {
        public override string EnglishName => "LamcpAutoStart";

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            var plugin = LamcpPlugin.Instance;
            plugin.AutoStart = !plugin.AutoStart;
            RhinoApp.WriteLine(
                "LAMCP bridge will {0}start automatically with Rhino.",
                plugin.AutoStart ? "" : "no longer ");
            return Result.Success;
        }
    }
}
