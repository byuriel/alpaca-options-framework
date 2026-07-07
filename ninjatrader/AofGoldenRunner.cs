// AofGoldenRunner.cs — conformance console app.
//
// Runs the C# engine (AofCore.cs) over a bars CSV and writes the per-bar
// state vectors in exactly the format the Python oracle emits
// (es_backtest.py --states). check_conformance.ps1 compiles this with the
// .NET Framework csc that ships with Windows, runs it, and diffs the output
// against the Python goldens with golden_vectors.py compare.
//
//   AofGoldenRunner.exe bars.csv states_out.csv [zone=off|grid] [spec=MES|ES]
//
// Accepts the same generic bar CSV the Python loader reads: a header with
// timestamp/open/high/low/close/volume (ISO-8601 timestamps, offset-aware
// or ET-naive) or Databento ts_event (ISO or integer nanoseconds).
// Timestamps are bar OPEN time.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using Aof;

public static class AofGoldenRunner
{
    private static readonly TimeZoneInfo Eastern = FindEastern();

    private static TimeZoneInfo FindEastern()
    {
        // Windows id first (the deployment target); IANA id for mono/Linux
        // so conformance can also run in CI
        try { return TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time"); }
        catch (TimeZoneNotFoundException)
        { return TimeZoneInfo.FindSystemTimeZoneById("America/New_York"); }
    }

    private static DateTime ParseEt(string raw)
    {
        raw = raw.Trim();
        long ns;
        if (long.TryParse(raw, NumberStyles.None, CultureInfo.InvariantCulture, out ns))
        {
            var utc = DateTimeOffset.FromUnixTimeMilliseconds(ns / 1000000L);
            return TimeZoneInfo.ConvertTime(utc, Eastern).DateTime;
        }
        // naive stamps (no Z / no offset after the time part) are ET already
        // — same convention as the Python loader
        bool hasOffset = raw.EndsWith("Z")
            || raw.IndexOf('+', 10) >= 0 || raw.IndexOf('-', 10) >= 0;
        if (!hasOffset)
            return DateTime.Parse(raw, CultureInfo.InvariantCulture,
                                  DateTimeStyles.None);
        var dto = DateTimeOffset.Parse(raw.Replace("Z", "+00:00"),
                                       CultureInfo.InvariantCulture,
                                       DateTimeStyles.None);
        return TimeZoneInfo.ConvertTime(dto, Eastern).DateTime;
    }

    public static int Main(string[] args)
    {
        if (args.Length < 2)
        {
            Console.Error.WriteLine(
                "usage: AofGoldenRunner bars.csv states_out.csv [zone] [spec]");
            return 2;
        }
        string barsPath = args[0], outPath = args[1];
        string zone = args.Length > 2 ? args[2] : "off";
        string spec = args.Length > 3 ? args[3] : "MES";
        double commission = spec == "ES" ? 3.20 : 1.30;   // config.py mirror

        var runner = new SessionRunner(zone, spec, commission);
        var inv = CultureInfo.InvariantCulture;

        using (var reader = new StreamReader(barsPath))
        using (var writer = new StreamWriter(outPath, false))
        {
            writer.NewLine = "\r\n";   // match Python's csv module default
            string header = reader.ReadLine();
            if (header == null) { Console.Error.WriteLine("empty bars file"); return 2; }
            var cols = header.Split(',');
            var idx = new Dictionary<string, int>();
            for (int i = 0; i < cols.Length; i++)
                idx[cols[i].Trim().ToLowerInvariant()] = i;

            int tCol = idx.ContainsKey("ts_event") ? idx["ts_event"]
                     : idx.ContainsKey("timestamp") ? idx["timestamp"]
                     : idx.ContainsKey("datetime") ? idx["datetime"]
                     : idx["time"];

            writer.WriteLine(string.Join(",", SessionRunner.StateColumns));

            string line;
            while ((line = reader.ReadLine()) != null)
            {
                if (line.Length == 0) continue;
                var p = line.Split(',');
                var bar = new AofBar
                {
                    TimeEt = ParseEt(p[tCol]),
                    Open   = double.Parse(p[idx["open"]],  inv),
                    High   = double.Parse(p[idx["high"]],  inv),
                    Low    = double.Parse(p[idx["low"]],   inv),
                    Close  = double.Parse(p[idx["close"]], inv),
                    Volume = idx.ContainsKey("volume")
                             ? double.Parse(p[idx["volume"]], inv) : 0.0,
                };
                var row = runner.OnBar(bar);
                if (row != null) writer.WriteLine(string.Join(",", row));
            }
        }

        Console.WriteLine("wrote " + outPath);
        return 0;
    }
}
