// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LocalTranslator",
    platforms: [.macOS("26.0")],
    products: [
        .library(name: "TranslatorCore", targets: ["TranslatorCore"]),
        .executable(name: "translator-m0", targets: ["TranslatorM0"]),
        .executable(name: "LocalTranslatorApp", targets: ["TranslatorApp"])
    ],
    dependencies: [
        .package(path: "Vendor/argmax-oss-swift"),
        .package(path: "Vendor/ZIPFoundation")
    ],
    targets: [
        .target(name: "TranslatorCore", dependencies: [
            .product(name: "WhisperKit", package: "argmax-oss-swift"),
            .product(name: "ZIPFoundation", package: "ZIPFoundation")
        ]),
        .executableTarget(name: "TranslatorM0", dependencies: ["TranslatorCore"]),
        .executableTarget(name: "TranslatorApp", dependencies: ["TranslatorCore"]),
        .testTarget(name: "TranslatorCoreTests", dependencies: ["TranslatorCore"])
    ],
    swiftLanguageModes: [.v5]
)
