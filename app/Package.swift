// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CompactlabApp",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "compactlab-app", targets: ["CompactlabApp"]),
    ],
    targets: [
        // Everything that can be tested without a window: the engine contract, the engine processes, scheduling and
        // what the panel shows. No SwiftUI here.
        .target(name: "CompactlabKit"),
        .executableTarget(name: "CompactlabApp", dependencies: ["CompactlabKit"]),
        .testTarget(name: "CompactlabKitTests", dependencies: ["CompactlabKit"]),
    ]
)
