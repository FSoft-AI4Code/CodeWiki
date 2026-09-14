## Purpose

Lets CodeWiki analyze and document Scala repositories on the same terms as the other
supported languages: recognizing Scala source files, extracting their architectural
components and dependency relationships, and classifying sbt build files.

## ADDED Requirements

### Requirement: Scala source file recognition

The system SHALL treat files with the `.scala` and `.sc` extensions as analyzable
source code, for both Scala 2 and Scala 3 syntax.

#### Scenario: Repository containing only Scala sources is accepted

- **WHEN** a user requests documentation for a repository whose only source files are `.scala` files
- **THEN** the repository passes validation instead of being rejected for containing no supported code

#### Scenario: Scala appears in detected language statistics

- **WHEN** a repository containing Scala files is scanned for language composition
- **THEN** Scala is reported with a file count reflecting its `.scala` and `.sc` files

#### Scenario: Scala 3 syntax is parsed

- **WHEN** a source file uses Scala 3 constructs such as `enum`, `given`, `extension`, or significant-indentation blocks
- **THEN** the file is parsed without error and its top-level definitions are extracted

### Requirement: Scala component extraction

The system SHALL extract Scala classes, case classes, traits, objects, package objects,
enums, and their members as documentable components, each carrying its name, source
location, source text, parameters, and documentation comment when present.

#### Scenario: Traits and objects are documentable architectural units

- **WHEN** a Scala file declares a trait and a standalone object
- **THEN** both are extracted as components eligible for documentation, not discarded

#### Scenario: Methods are attributed to their enclosing type

- **WHEN** a method is declared inside a class, trait, or object
- **THEN** the resulting component identifies that enclosing type as its owner

#### Scenario: Top-level definitions are extracted as functions

- **WHEN** a Scala file declares a definition outside any class, trait, or object
- **THEN** it is extracted as a free function component

#### Scenario: Documentation comments are captured

- **WHEN** a Scala declaration is preceded by a Scaladoc or line comment
- **THEN** the component records that text as its documentation

### Requirement: Companion declarations remain distinct

A class or trait and its same-named companion object SHALL both be preserved as
separate components with distinct identifiers. Neither declaration may displace the
other.

#### Scenario: Class and companion object both survive

- **WHEN** a file declares both `class Buffer` and `object Buffer`
- **THEN** two distinct components exist, each retaining its own source range, documentation, and members

#### Scenario: Members are attributed to the correct companion

- **WHEN** `class Buffer` declares `push` and `object Buffer` declares `apply`
- **THEN** `push` is attributed to the class and `apply` to the object, not both to one of them

### Requirement: Scala dependency and call relationships

The system SHALL emit dependency relationships for Scala code covering inheritance,
trait mixins, constructor parameter and field types, instantiation, and method calls.
Relationships whose target is defined in the analyzed repository SHALL resolve to that
component; relationships to symbols outside the repository SHALL be retained as
unresolved logical names.

#### Scenario: Inheritance produces a dependency edge

- **WHEN** a class extends a base class defined in another file in the repository
- **THEN** a resolved dependency edge links the subclass to that base class

#### Scenario: Trait mixins produce dependency edges

- **WHEN** a class mixes in a trait defined in the repository
- **THEN** a resolved dependency edge links the class to that trait

#### Scenario: Intra-file calls resolve to sibling components

- **WHEN** a method calls another method on the same type
- **THEN** a resolved dependency edge links the two method components

#### Scenario: External symbols stay unresolved

- **WHEN** Scala code references a type or method from the standard library or a third-party dependency
- **THEN** the relationship is recorded as an unresolved logical name rather than a repository component

#### Scenario: Standard-library noise is excluded

- **WHEN** Scala code uses ubiquitous built-ins such as primitive types or common collection operations
- **THEN** those references do not appear as dependency edges

### Requirement: sbt build files are classified as build artifacts

The system SHALL classify `build.sbt` and `.sbt` files under a `project/` directory as
build manifests, so that sbt-based projects have their build and packaging story
documented alongside the existing Maven and Gradle handling.

#### Scenario: build.sbt is treated as a manifest

- **WHEN** a repository contains a `build.sbt` at its root
- **THEN** that file is classified as a build manifest artifact rather than ignored or documented as application source

#### Scenario: sbt plugin definitions are treated as build files

- **WHEN** a repository contains `project/plugins.sbt`
- **THEN** that file is classified as a build artifact

### Requirement: End-to-end documentation generation for Scala repositories

A Scala repository SHALL produce documentation describing its actual components. Scala
files may not be silently dropped between discovery and analysis.

#### Scenario: Scala components reach the generated documentation

- **WHEN** documentation is generated for a repository of Scala source files
- **THEN** the generated output describes components extracted from those files

#### Scenario: Scala code snippets are tagged for highlighting

- **WHEN** generated documentation embeds a code snippet taken from a Scala source file
- **THEN** the snippet is tagged as Scala so it renders with Scala syntax highlighting

### Requirement: Incremental regeneration reacts to Scala edits

The system SHALL detect modifications to Scala source files when determining whether a
repository has changed since its last documentation run.

#### Scenario: A modified Scala file marks the repository as changed

- **WHEN** a `.scala` file is modified after the previous documentation run
- **THEN** the change detection reports that file as changed
