// swift-tools-version: 5.10
// LocalTranslator: retain only the two library targets used by the M0 app.
// Original manifest and licensing are retained alongside this file.
import PackageDescription
let package = Package(
    name: "argmax-oss-swift", platforms: [.macOS(.v13)],
    products: [.library(name: "WhisperKit", targets: ["WhisperKit"])],
    targets: [
        .target(name: "ArgmaxCore", swiftSettings: [.enableExperimentalFeature("StrictConcurrency")]),
        .target(name: "WhisperKit", dependencies: ["ArgmaxCore"], swiftSettings: [.enableExperimentalFeature("StrictConcurrency")])
    ], swiftLanguageVersions: [.v5]
)
