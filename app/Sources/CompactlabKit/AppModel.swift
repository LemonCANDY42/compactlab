import Foundation
import Observation
import os

/// The app's state and everything it asks the engine. One instance, on the main actor.
///
/// Engine processes: `hello` at start; one resident `watch` for the active sessions (FSEvents → `refresh`); `result`,
/// `plan` and `history` after start and after each calibration or change; `calibrate` when due (the service's schedule
/// and backoff, put off on low power) or when asked; `apply` / `revert` when the person clicks; `verify` at most daily
/// for the latest change. Nothing here parses logs, writes files of other tools or uses the network: the engine's
/// `apply` is the only writer of settings.
@MainActor
@Observable
public final class AppModel {
    public enum EngineStatus: Equatable, Sendable {
        case starting
        case missing
        /// The engine speaks another contract version: shown, but nothing will be written.
        case incompatible(api: Int, version: String)
        case ready(version: String)
        case failed(String)

        public var isUsable: Bool {
            switch self {
            case .ready, .incompatible: true
            default: false
            }
        }

        /// Commands that write (a calibration writes the result, the lens and the service state) need the contract
        /// this app was built for.
        public var canWrite: Bool {
            if case .ready = self { return true }
            return false
        }
    }

    public enum WatchStatus: Equatable, Sendable {
        case off
        case running
        case restarting
        case stopped(String)
    }

    public enum CalibrationStatus: Equatable, Sendable {
        case idle
        case running(since: Date)
        /// Another process holds the lock (the CLI, a schedule): nothing ran.
        case busy(String)
        case failed(String)
    }

    // Settings (UserDefaults).
    public var enginePath: String {
        didSet { defaults.set(enginePath, forKey: Keys.enginePath) }
    }
    public var showProjects: Bool {
        didSet {
            defaults.set(showProjects, forKey: Keys.showProjects)
            if oldValue != showProjects { restartWatch() }
        }
    }
    public var showPercent: Bool {
        didSet { defaults.set(showPercent, forKey: Keys.showPercent) }
    }
    /// The profile the threshold table shows; empty: the one the result was selected with.
    public var displayProfile: String {
        didSet {
            defaults.set(displayProfile, forKey: Keys.displayProfile)
            if oldValue != displayProfile {
                Task {
                    await loadResult()
                    await loadPlan()
                }
            }
        }
    }
    public var notifySuggestions: Bool {
        didSet { defaults.set(notifySuggestions, forKey: Keys.notifySuggestions) }
    }
    public var notifyChanges: Bool {
        didSet { defaults.set(notifyChanges, forKey: Keys.notifyChanges) }
    }
    public var notifyProblems: Bool {
        didSet { defaults.set(notifyProblems, forKey: Keys.notifyProblems) }
    }

    public private(set) var status: EngineStatus = .starting
    public private(set) var executable: URL?
    public private(set) var hello: Hello?
    public private(set) var sessions: SessionsSnapshot?
    public private(set) var watchStatus: WatchStatus = .off
    public private(set) var watchError: String?
    public private(set) var result: ResultSummary?
    public private(set) var resultError: EngineError?
    public private(set) var calibration: CalibrationStatus = .idle
    public private(set) var reportError: String?
    /// What `apply` would change now (nil: not loaded, or the engine cannot write settings).
    public private(set) var plan: Plan?
    public private(set) var planError: EngineError?
    /// The latest applies, newest first, with what each key holds now.
    public private(set) var history: History?
    /// The verification of the newest apply (kept between launches).
    public private(set) var verification: Verification?
    /// An apply, revert or consent change is running.
    public private(set) var isWriting = false
    /// The outcome of the last apply or revert when it needs saying (a refusal, a partial undo).
    public private(set) var writeMessage: String?
    /// Moves when something (a notification) asks for the review window.
    public private(set) var reviewRequest = 0
    /// Posts a notification (the app sets it; tests record).
    public var post: @MainActor (Notice) -> Void = { _ in }
    /// Moves once a minute so time-based text (cache going cold, "2 h ago") and the icon stay current.
    public private(set) var now = Date()

    private let defaults: UserDefaults
    private let baseEnvironment: [String: String]
    private let log = Logger(subsystem: "io.github.lemoncandy42.compactlab", category: "model")
    private var locator: EngineLocator { EngineLocator(customPath: enginePath, environment: baseEnvironment) }
    private var watch: WatchProcess?
    private var watchTask: Task<Void, Never>?
    private var restartPolicy = RestartPolicy()
    private var stoppingWatch: ObjectIdentifier?
    private var calibrateTask: Task<Void, Never>?
    private var fileWatcher: FileWatcher?
    private var ticker: Timer?
    private var lastScheduleCheck = Date.distantPast
    private var lastHelloRefresh = Date.distantPast
    private var lastHistoryLoad = Date.distantPast
    private var noticeState: NoticeState
    /// A timed-out or cancelled calibration leaves no record in the service state (the engine writes it when a run
    /// ends), so the app backs off on its own: 5 minutes, doubling, up to the interval.
    private var interrupted = 0
    private var notBefore = Date.distantPast
    /// Checked before a scheduled calibration; replaceable in tests.
    public var powerState: @Sendable () -> PowerState = { PowerState.current() }
    /// Whether scheduled calibrations run (tests turn them off).
    public var scheduling = true
    /// A calibration running longer is stopped.
    public var calibrateTimeout: Duration = .seconds(600)
    /// Service state read this recently is not read again before a scheduled run.
    public var serviceFreshFor: TimeInterval = 60

    public init(defaults: UserDefaults = .standard, environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.defaults = defaults
        self.baseEnvironment = environment
        enginePath = defaults.string(forKey: Keys.enginePath) ?? ""
        showProjects = defaults.object(forKey: Keys.showProjects) as? Bool ?? true
        showPercent = defaults.bool(forKey: Keys.showPercent)
        displayProfile = defaults.string(forKey: Keys.displayProfile) ?? ""
        notifySuggestions = defaults.object(forKey: Keys.notifySuggestions) as? Bool ?? true
        notifyChanges = defaults.object(forKey: Keys.notifyChanges) as? Bool ?? true
        notifyProblems = defaults.object(forKey: Keys.notifyProblems) as? Bool ?? true
        noticeState = defaults.data(forKey: Keys.notices).flatMap { try? JSONDecoder().decode(NoticeState.self, from: $0) } ?? NoticeState()
        verification = defaults.data(forKey: Keys.verification).flatMap { try? JSONDecoder().decode(Verification.self, from: $0) }
    }

    // MARK: Lifecycle

    /// Find the engine, shake hands, start watching, load the last result, calibrate if due.
    public func start() async {
        stopWatch()
        status = .starting
        hello = nil
        guard let exe = locator.locate() else {
            executable = nil
            status = .missing
            return
        }
        executable = exe
        do {
            let out = try await run(["engine", "hello"], timeout: .seconds(30))
            switch try EngineAPI.decode(out.stdout) {
            case .hello(let h):
                hello = h
                lastHelloRefresh = Date()
                status = h.engineApi == EngineAPI.version ? .ready(version: h.version) : .incompatible(api: h.engineApi, version: h.version)
            case .error(let e):
                status = .failed(e.message)
                return
            default:
                status = .failed("unexpected reply to engine hello")
                return
            }
        } catch {
            status = .failed(describe(error, stderr: nil))
            return
        }
        startTicker()
        startWatch()
        await loadResult()
        await loadSettings()
        await calibrateIfDue()
    }

    /// Stop the engine processes (app quitting).
    public func shutdown() {
        ticker?.invalidate()
        ticker = nil
        calibrateTask?.cancel()
        stopWatch()
    }

    // MARK: Watch

    public func restartWatch() {
        stopWatch()
        restartPolicy.reset()
        startWatch()
    }

    private func startWatch() {
        stopWatch()  // two overlapping start()s must not leave a watcher running
        guard let exe = executable, let h = hello, h.commands.contains("watch") else {
            watchStatus = .off
            return
        }
        var args = ["engine", "watch"]
        if !showProjects { args.append("--no-project-names") }
        let w = WatchProcess(executable: exe, arguments: args, environment: locator.engineEnvironment)
        do {
            try w.start()
        } catch {
            watchStatus = .stopped("could not start the engine: \(error.localizedDescription)")
            return
        }
        watch = w
        watchStatus = .running
        watchError = nil
        watchTask = Task { [weak self] in
            for await event in w.events {
                guard let self else { return }
                self.handle(event, from: w)
            }
        }
        if fileWatcher?.paths != h.liveRoots {
            fileWatcher?.stop()
            let fw = FileWatcher { [weak self] in Task { @MainActor in self?.watch?.send("refresh") } }
            fw.watch(h.liveRoots)
            fileWatcher = fw
        }
    }

    private func stopWatch() {
        guard let w = watch else { return }
        stoppingWatch = ObjectIdentifier(w)
        watch = nil
        w.stop()
        watchStatus = .off
    }

    private func handle(_ event: WatchProcess.Event, from w: WatchProcess) {
        switch event {
        case .line(let data):
            guard watch === w else { return }
            switch try? EngineAPI.decode(data) {
            case .sessions(let s):
                sessions = s
                watchError = nil
            case .error(let e):
                watchError = e.message
            case nil:
                watchError = "the engine printed something that is not JSON"
                log.error("watch: undecodable line (\(data.count) bytes)")
            default:
                break
            }
        case .exited(let status):
            if stoppingWatch == ObjectIdentifier(w) {
                stoppingWatch = nil
                return
            }
            guard watch === w else { return }
            watch = nil
            let why = w.lastStderr.split(whereSeparator: \.isNewline).last.map(String.init) ?? "exit status \(status)"
            log.error("watch exited (\(status)): \(why, privacy: .public)")
            if restartPolicy.shouldRestart(at: Date()) {
                watchStatus = .restarting
                Task { [weak self] in
                    try? await Task.sleep(for: .seconds(1))
                    guard let self, self.watch == nil, self.watchStatus == .restarting else { return }
                    self.startWatch()
                }
            } else {
                watchStatus = .stopped("The session watcher stopped 3 times in a minute (\(why)).")
            }
        }
    }

    // MARK: Result and calibration

    public func loadResult() async {
        guard status.isUsable else { return }
        var args = ["engine", "result"]
        if !displayProfile.isEmpty { args += ["--profile", displayProfile] }
        do {
            let out = try await run(args, timeout: .seconds(60))
            switch try EngineAPI.decode(out.stdout) {
            case .result(let r):
                result = r
                resultError = nil
            case .error(let e):
                if e.code == "no_result" { result = nil }
                resultError = e
            default:
                resultError = EngineError(code: "internal", message: "unexpected reply to engine result")
            }
        } catch {
            resultError = EngineError(code: "internal", message: describe(error, stderr: nil))
        }
    }

    /// Run a calibration now (the panel's button). Scheduled ones go through `calibrateIfDue`.
    public func recalibrate(reason: String = "app") {
        guard status.canWrite, calibrateTask == nil else { return }
        calibration = .running(since: Date())
        calibrateTask = Task { [weak self] in
            guard let self else { return }
            await self.calibrate(reason: reason)
            self.calibrateTask = nil
        }
    }

    public func cancelCalibration() {
        calibrateTask?.cancel()
    }

    public var isCalibrating: Bool {
        if case .running = calibration { return true }
        return false
    }

    private func calibrate(reason: String) async {
        do {
            var args = ["engine", "calibrate", "--reason", reason]
            if canChangeSettings, hello?.consent != nil { args.append("--auto-apply") }
            let out = try await run(args, timeout: calibrateTimeout)
            if out.cancelled || out.timedOut {
                interrupted += 1
                let every = result?.calibration?.everyS ?? Schedule.defaultEvery
                notBefore = Date().addingTimeInterval(
                    min(every, Schedule.firstBackoff * pow(2, Double(interrupted - 1))))
                calibration = out.cancelled ? .idle : .failed("the calibration ran too long and was stopped")
                return
            }
            interrupted = 0
            notBefore = .distantPast
            switch try? EngineAPI.decode(out.stdout) {
            case .result:
                calibration = .idle
            case .error(let e) where e.code == "busy":
                calibration = .busy(e.message)
            case .error(let e):
                calibration = .failed(e.message)
            default:
                calibration = .failed(out.stderrTail.isEmpty ? "exit status \(out.status)" : out.stderrTail)
            }
        } catch {
            calibration = .failed(describe(error, stderr: nil))
        }
        await refreshHello()
        await loadResult()
        await loadSettings()
        await verifyIfDue()
    }

    /// Calibrate when the service schedule says so, unless saving power. Called at start, each tick and on wake.
    public func calibrateIfDue() async {
        guard scheduling, status.canWrite, calibrateTask == nil, Date() >= notBefore, hello != nil else { return }
        // A calibration without an interval (`serve --every off`) means: manual only, as the engine's schedule has it.
        let every: TimeInterval
        if let c = result?.calibration {
            guard let e = c.everyS else { return }
            every = e
        } else {
            every = Schedule.defaultEvery
        }
        // The CLI, `serve` or a schedule may have calibrated (or failed) since: their bookkeeping moves the due time.
        // Re-read it every half hour, and before starting a run unless it was read within `serviceFreshFor`.
        if Date().timeIntervalSince(lastHelloRefresh) >= 1800 { await refreshHello() }
        guard let h = hello, Schedule.isDue(service: h.service, every: every, now: Date()) else { return }
        if powerState().shouldDefer {
            log.info("calibration due but put off: low power")
            return
        }
        if case .busy = calibration, Date().timeIntervalSince(lastScheduleCheck) < 60 { return }
        if Date().timeIntervalSince(lastHelloRefresh) >= serviceFreshFor {
            await refreshHello()
            guard calibrateTask == nil, let h = hello, Schedule.isDue(service: h.service, every: every, now: Date())
            else { return }
        }
        lastScheduleCheck = Date()
        recalibrate(reason: "app-schedule")
    }

    /// The service bookkeeping (last calibration, failures) changes with every calibration, wherever it ran.
    private func refreshHello() async {
        guard let out = try? await run(["engine", "hello"], timeout: .seconds(30)),
            case .hello(let h) = try? EngineAPI.decode(out.stdout)
        else { return }
        hello = h
        lastHelloRefresh = Date()
    }

    /// Wake from sleep: timers are reset by the system; catch up on a missed calibration (once).
    public func didWake() {
        now = Date()
        watch?.send("snapshot")
        Task { await calibrateIfDue() }
    }

    // MARK: Changing settings

    /// The engine can write settings and the result is real (not the demo's).
    public var canChangeSettings: Bool {
        status.canWrite && (hello?.canChangeSettings ?? false) && result?.demo != true
    }

    public var autoApply: Bool { hello?.consent != nil }

    /// Something waits for the person: a suggestion two calibrations agree on, or a setting changed by someone else.
    public var hasPending: Bool {
        Notices.pending(plan: plan, result: result, history: history, state: noticeState, now: now)
    }

    /// Suggested changes the last two calibrations agree on (the ones a notification is about).
    public var stableChanges: [Plan.Change] {
        Notices.stable(plan, result: result, state: noticeState, now: now)
    }

    /// Plan and history, then the notices they call for.
    public func loadSettings() async {
        await loadPlan()
        await loadHistory()
        updateNotices()
    }

    public func loadPlan() async {
        guard canChangeSettings, result != nil else {
            plan = nil
            return
        }
        var args = ["engine", "plan"]
        if !displayProfile.isEmpty { args += ["--profile", displayProfile] }
        switch await oneShot(args, timeout: .seconds(60)) {
        case .success(.plan(let p)):
            plan = p
            planError = nil
        case .success(.error(let e)):
            plan = nil
            planError = e
        case .success:
            planError = EngineError(code: "internal", message: "unexpected reply to engine plan")
        case .failure(let e):
            planError = e
        }
    }

    public func loadHistory() async {
        guard status.isUsable, hello?.commands.contains("history") == true else { return }
        lastHistoryLoad = Date()
        if case .success(.history(let h)) = await oneShot(["engine", "history", "--limit", "5"], timeout: .seconds(30)) {
            history = h
        }
    }

    /// Write one suggested change, or all of them (nil). The engine plans again under its lock and writes only if the
    /// plan is still the one shown.
    public func apply(_ change: Plan.Change?) async {
        guard canChangeSettings, let plan, !isWriting else { return }
        isWriting = true
        writeMessage = nil
        var args = ["engine", "apply"]
        if !displayProfile.isEmpty { args += ["--profile", displayProfile] }
        if let c = change {
            args += ["--only", c.selector, "--expect-plan", c.planHash]
        } else {
            args += ["--expect-plan", plan.planHash]
        }
        switch await oneShot(args, timeout: .seconds(60)) {
        case .success(.applied):
            break
        case .success(.error(let e)):
            writeMessage = refusal(e)
        case .success:
            writeMessage = L("The engine's reply could not be read; check the settings file.")
        case .failure(let e):
            writeMessage = e.message
        }
        isWriting = false
        await loadSettings()
    }

    /// Undo an apply from its manifest (only the keys nobody changed since).
    public func revert(_ id: String) async {
        guard status.canWrite, hello?.canChangeSettings == true, !isWriting else { return }
        isWriting = true
        writeMessage = nil
        switch await oneShot(["engine", "revert", "--id", id], timeout: .seconds(60)) {
        case .success(.reverted(let r)):
            if !r.complete { writeMessage = L("Undone, except settings someone changed since: they were left as they are.") }
        case .success(.error(let e)):
            writeMessage = refusal(e)
        case .success:
            writeMessage = L("The engine's reply could not be read; check the settings file.")
        case .failure(let e):
            writeMessage = e.message
        }
        isWriting = false
        await loadSettings()
    }

    /// Turn auto-apply (Claude Code, within the engine's limits) on or off: the engine's stored consent.
    public func setAutoApply(_ on: Bool) async {
        guard canChangeSettings, !isWriting else { return }
        isWriting = true
        let args = on ? ["engine", "consent", "set", "--profile", result?.profile ?? "balanced"] : ["engine", "consent", "clear"]
        if case .success(.error(let e)) = await oneShot(args, timeout: .seconds(30)) { writeMessage = e.message }
        isWriting = false
        await refreshHello()
    }

    /// "Remind me in a week": no notification or menu bar dot for these suggestions until then.
    public func snooze(_ changes: [Plan.Change]) {
        let until = Date().addingTimeInterval(Notices.snooze).timeIntervalSince1970
        for c in changes { noticeState.snoozed[Notices.suggestionId(c)] = until }
        saveNotices()
    }

    public func requestReview() { reviewRequest += 1 }

    public func clearWriteMessage() { writeMessage = nil }

    /// Check the newest apply against the logs since: once it is a day old, at most daily, until the verdict is final.
    public func verifyIfDue() async {
        guard status.isUsable, hello?.commands.contains("verify") == true, let newest = history?.applies.first,
            newest.inEffect, let at = newest.at, calibrateTask == nil
        else { return }
        let age = Date().timeIntervalSince1970 - at
        guard age >= 86_400 else { return }
        if let v = verification, v.id == newest.id, v.isFinal { return }
        let ran = defaults.double(forKey: Keys.verifiedAt)
        guard Date().timeIntervalSince1970 - ran >= 20 * 3600 else { return }
        defaults.set(Date().timeIntervalSince1970, forKey: Keys.verifiedAt)
        let days = min(60, Int((2 * age / 86_400 + 7).rounded(.up)))  // enough before the change to compare with
        let args = ["engine", "verify", "--id", newest.id, "--since-days", String(days)]
        if case .success(.verification(let v)) = await oneShot(args, timeout: .seconds(600)) {
            verification = v
            defaults.set(try? JSONEncoder().encode(v), forKey: Keys.verification)
            updateNotices()
        }
    }

    private func updateNotices() {
        let enabled = Notices.Enabled(suggestions: notifySuggestions, changes: notifyChanges, problems: notifyProblems)
        let notices = Notices.evaluate(
            plan: plan, result: result, history: history, verification: verification, service: hello?.service,
            enabled: enabled, state: &noticeState, now: Date())
        saveNotices()
        for n in notices { post(n) }
    }

    private func saveNotices() {
        defaults.set(try? JSONEncoder().encode(noticeState), forKey: Keys.notices)
    }

    private func refusal(_ e: EngineError) -> String {
        switch e.code {
        case "plan_changed": L("The settings or the suggestion changed since this was shown. Review the new plan.")
        case "busy": L("A calibration or another change is running. Try again in a minute.")
        default: e.message
        }
    }

    /// Run a one-shot engine command and decode its one object.
    private func oneShot(_ args: [String], timeout: Duration) async -> Result<EngineMessage, EngineError> {
        do {
            let out = try await run(args, timeout: timeout)
            if out.timedOut { return .failure(EngineError(code: "timeout", message: L("The engine took too long."))) }
            return .success(try EngineAPI.decode(out.stdout))
        } catch let e as EngineError {
            return .failure(e)
        } catch {
            return .failure(EngineError(code: "internal", message: describe(error, stderr: nil)))
        }
    }

    // MARK: Report

    /// Render the offline HTML report of the last calibration; returns the file to open.
    public func renderReport() async -> URL? {
        reportError = nil
        guard let h = hello else { return nil }
        let dir = URL(fileURLWithPath: (h.stateDir as NSString).expandingTildeInPath).appendingPathComponent("app")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("report.html")
        var args = ["report", "--result", h.result.expanded, "--out", file.path]
        if !displayProfile.isEmpty { args += ["--profile", displayProfile] }
        do {
            let out = try await run(args, timeout: .seconds(120))
            guard out.status == 0, FileManager.default.fileExists(atPath: file.path) else {
                reportError = out.stderrTail.isEmpty ? "the report could not be made" : out.stderrTail
                return nil
            }
            return file
        } catch {
            reportError = describe(error, stderr: nil)
            return nil
        }
    }

    // MARK: Derived

    public var icon: IconState {
        Panel.icon(engineOK: status.isUsable && !isWatchStopped, sessions: sessions, now: now)
    }

    public var isWatchStopped: Bool {
        if case .stopped = watchStatus { return true }
        return false
    }

    public var sessionRows: [SessionRow] {
        sessions.map { Panel.sessionRows($0, now: now, showProjects: showProjects) } ?? []
    }

    public var thresholdRows: [ThresholdRow] {
        result.map(Panel.thresholdRows) ?? []
    }

    /// Diagnostics to paste into an issue: versions, paths, states; no session content or project names.
    public var diagnostics: String {
        var lines = [
            "compactlab app \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev")",
            "macOS \(ProcessInfo.processInfo.operatingSystemVersionString)",
            "engine: \(executable?.path ?? "not found") — \(status)",
        ]
        if let h = hello {
            lines.append("engine \(h.version), api \(h.engineApi), python \(h.python ?? "?")")
            lines.append("live: \(h.liveHarnesses.joined(separator: ", ")); watching \(h.liveRoots.count) directories")
            lines.append(
                "service: last calibrated \(h.service.lastCalibrated.map { "\(Date(timeIntervalSince1970: $0))" } ?? "never"), failures \(h.service.failures), last error \(h.service.lastError ?? "none")"
            )
        }
        lines.append("watch: \(watchStatus)\(watchError.map { " (\($0))" } ?? "")")
        lines.append("sessions: \(sessions?.sessions.count ?? 0), unreadable files \(sessions?.unreadable ?? 0)")
        lines.append("calibration: \(calibration); result: \(result == nil ? (resultError?.code ?? "none") : "loaded")")
        return lines.joined(separator: "\n")
    }

    // MARK: Helpers

    private func startTicker() {
        guard ticker == nil else { return }
        let t = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.now = Date()
                await self.calibrateIfDue()
                // a setting edited by hand shows up within half an hour
                if self.now.timeIntervalSince(self.lastHistoryLoad) >= 1800 { await self.loadSettings() }
            }
        }
        t.tolerance = 10
        RunLoop.main.add(t, forMode: .common)
        ticker = t
    }

    private func run(_ args: [String], timeout: Duration) async throws -> ProcessOutput {
        guard let exe = executable else { throw EngineError(code: "missing", message: "compactlab was not found") }
        return try await ProcessRunner.run(exe, args, environment: locator.engineEnvironment, timeout: timeout)
    }

    private func describe(_ error: Error, stderr: String?) -> String {
        if let e = error as? EngineError { return e.message }
        if let d = error as? DecodingError { return "the engine's reply could not be read (\(d))" }
        return stderr ?? error.localizedDescription
    }

    enum Keys {
        static let enginePath = "enginePath"
        static let showProjects = "showProjectNames"
        static let showPercent = "showPercentInMenuBar"
        static let displayProfile = "displayProfile"
        static let notifySuggestions = "notifySuggestions"
        static let notifyChanges = "notifyChanges"
        static let notifyProblems = "notifyProblems"
        static let notices = "noticeState"
        static let verification = "verification"
        static let verifiedAt = "verificationRunAt"
    }
}
