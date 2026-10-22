using System.Globalization;
using System.Text;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

// This process only reads LibreHardwareMonitor sensors and writes JSON lines to stdout.
// No power limits, fan controls, undervolting or MSR writes are performed.
internal static class Program
{
    private static readonly CancellationTokenSource Stop = new();

    private static int Main(string[] args)
    {
        bool once = false;
        int intervalMs = 1000;
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--once":
                    once = true;
                    break;
                case "--interval-ms":
                    if (++i >= args.Length ||
                        !int.TryParse(args[i], NumberStyles.Integer, CultureInfo.InvariantCulture, out intervalMs) ||
                        intervalMs < 250 || intervalMs > 60000)
                    {
                        Console.Error.WriteLine("--interval-ms must be an integer from 250 to 60000.");
                        return 2;
                    }
                    break;
                case "--help":
                case "-h":
                    Console.WriteLine("SurfaceSensors --once | [--interval-ms 1000]");
                    Console.WriteLine("Emits read-only LibreHardwareMonitor sensor samples as NDJSON.");
                    return 0;
                default:
                    Console.Error.WriteLine($"Unknown option: {args[i]}");
                    return 2;
            }
        }

        Console.OutputEncoding = new UTF8Encoding(false);
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; Stop.Cancel(); };

        var computer = new Computer
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMotherboardEnabled = true,
            IsMemoryEnabled = true
        };

        try
        {
            computer.Open();
            long sequence = 0;
            do
            {
                var sensors = new List<object>();
                foreach (var hardware in computer.Hardware)
                {
                    ReadHardware(hardware, sensors);
                }

                var packet = new
                {
                    kind = "sample",
                    timestamp_utc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                    sequence = ++sequence,
                    sensors
                };
                Console.WriteLine(JsonSerializer.Serialize(packet));
                Console.Out.Flush();

                if (once || Stop.IsCancellationRequested)
                    break;

                try
                {
                    Task.Delay(intervalMs, Stop.Token).GetAwaiter().GetResult();
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            } while (!Stop.IsCancellationRequested);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Sensor bridge error: {ex.GetType().Name}: {ex.Message}");
            return 1;
        }
        finally
        {
            try { computer.Close(); }
            catch (Exception ex) { Console.Error.WriteLine($"Close warning: {ex.Message}"); }
        }
    }

    private static void ReadHardware(IHardware hardware, List<object> result)
    {
        try
        {
            hardware.Update();
            foreach (var sensor in hardware.Sensors)
            {
                float? value = sensor.Value;
                if (!value.HasValue || !float.IsFinite(value.Value))
                    continue;

                result.Add(new
                {
                    id = sensor.Identifier.ToString(),
                    hardware_id = hardware.Identifier.ToString(),
                    hardware_name = hardware.Name,
                    hardware_type = hardware.HardwareType.ToString(),
                    name = sensor.Name,
                    sensor_type = sensor.SensorType.ToString(),
                    unit = UnitFor(sensor.SensorType),
                    value = value.Value
                });
            }
            foreach (var child in hardware.SubHardware)
                ReadHardware(child, result);
        }
        catch (Exception ex)
        {
            // An inaccessible device should not stop the other sensor streams.
            Console.Error.WriteLine($"Hardware warning ({hardware.Name}): {ex.Message}");
        }
    }

    private static string UnitFor(SensorType type) => type switch
    {
        SensorType.Temperature => "C",
        SensorType.Clock => "MHz",
        SensorType.Power => "W",
        SensorType.Load => "%",
        SensorType.Voltage => "V",
        SensorType.Fan => "RPM",
        SensorType.Control => "%",
        SensorType.Current => "A",
        SensorType.Energy => "mWh",
        _ => ""
    };
}
