import Foundation
import IOKit.ps

/// When the next background calibration is due, following the engine's own service rules (docs/SERVICE.md): every
/// `every` seconds after the last success; after failures, back off from five minutes, doubling, up to `every`.
public enum Schedule {
    public static let defaultEvery: TimeInterval = 86_400
    public static let firstBackoff: TimeInterval = 300

    /// `service/schedule.py` `next_due`: after a failure, the last attempt plus the backoff; never calibrated, now;
    /// otherwise the last success plus the interval.
    public static func nextRun(service: ServiceState, every: TimeInterval = defaultEvery) -> Date {
        if service.failures > 0, let attempt = service.lastAttempt {
            return Date(timeIntervalSince1970: attempt + min(every, firstBackoff * pow(2, Double(service.failures - 1))))
        }
        guard let last = service.lastCalibrated else { return .distantPast }
        return Date(timeIntervalSince1970: last + every)
    }

    public static func isDue(service: ServiceState, every: TimeInterval = defaultEvery, now: Date = Date()) -> Bool {
        now >= nextRun(service: service, every: every)
    }
}

/// Whether to put off a scheduled calibration to save power: Low Power Mode, or on battery below 20%.
public struct PowerState: Sendable, Equatable {
    public var lowPowerMode: Bool
    public var onBattery: Bool
    public var batteryPercent: Int?

    public init(lowPowerMode: Bool, onBattery: Bool, batteryPercent: Int?) {
        self.lowPowerMode = lowPowerMode
        self.onBattery = onBattery
        self.batteryPercent = batteryPercent
    }

    public var shouldDefer: Bool {
        lowPowerMode || (onBattery && (batteryPercent ?? 100) < 20)
    }

    public static func current() -> PowerState {
        let low = ProcessInfo.processInfo.isLowPowerModeEnabled
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue() else {
            return PowerState(lowPowerMode: low, onBattery: false, batteryPercent: nil)
        }
        let type = IOPSGetProvidingPowerSourceType(info)?.takeUnretainedValue() as String?
        let onBattery = type == kIOPSBatteryPowerValue
        var percent: Int?
        if let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef] {
            for src in list {
                guard let d = IOPSGetPowerSourceDescription(info, src)?.takeUnretainedValue() as? [String: Any],
                    let cur = d[kIOPSCurrentCapacityKey] as? Int, let max = d[kIOPSMaxCapacityKey] as? Int, max > 0
                else { continue }
                percent = cur * 100 / max
            }
        }
        return PowerState(lowPowerMode: low, onBattery: onBattery, batteryPercent: percent)
    }
}
