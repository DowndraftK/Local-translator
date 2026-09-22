// swift-tools-version: 5.10
// LocalTranslator macOS-only library manifest; original is Package.upstream.swift.
import PackageDescription
let package = Package(name: "ZIPFoundation", platforms: [.macOS(.v13)],
    products: [.library(name: "ZIPFoundation", targets: ["ZIPFoundation"])],
    targets: [.target(name: "ZIPFoundation", resources: [.copy("Resources/PrivacyInfo.xcprivacy")])],
    swiftLanguageVersions: [.v5])
