"""Flutter, Riverpod, Bloc and GoRouter knowledge for the Dart analyzer.

Pure tables and classifiers; the analyzer decides where to apply them.
Classification is by declared supertype name, so only direct framework
subclasses and same-file subclass chains are recognised.
"""

import re

FLUTTER_WIDGET_BASES = frozenset(
    {
        "StatelessWidget",
        "StatefulWidget",
        "ConsumerWidget",
        "ConsumerStatefulWidget",
        "HookWidget",
        "HookConsumerWidget",
        "StatefulHookWidget",
        "StatefulHookConsumerWidget",
        "InheritedWidget",
        "InheritedNotifier",
        "InheritedModel",
        "RenderObjectWidget",
        "LeafRenderObjectWidget",
        "SingleChildRenderObjectWidget",
        "MultiChildRenderObjectWidget",
        "ProxyWidget",
        "ParentDataWidget",
    }
)
FLUTTER_STATE_BASES = frozenset({"State", "ConsumerState"})
STATE_MANAGEMENT_BASES = {
    "ChangeNotifier": "notifier",
    "ValueNotifier": "notifier",
    "Notifier": "notifier",
    "AsyncNotifier": "notifier",
    "StreamNotifier": "notifier",
    "FamilyNotifier": "notifier",
    "FamilyAsyncNotifier": "notifier",
    "AutoDisposeNotifier": "notifier",
    "AutoDisposeAsyncNotifier": "notifier",
    "StateNotifier": "notifier",
    "Bloc": "bloc",
    "HydratedBloc": "bloc",
    "Cubit": "cubit",
    "HydratedCubit": "cubit",
}
RIVERPOD_ANNOTATIONS = frozenset({"riverpod", "Riverpod"})
REF_METHODS = frozenset(
    {"watch", "read", "listen", "listenManual", "invalidate", "refresh", "exists"}
)
# Methods whose instantiations are the widget's composition: build(),
# helper builders (buildHeader, _buildRow) and createState().
COMPOSITION_METHOD_RE = re.compile(r"^(_?build\w*|createState)$")
_INITIALIZER_CTOR_RE = re.compile(
    r"\A\s*(?:const\s+|new\s+)?([A-Z]\w*)(?:\.\w+)*\s*(?:<[^()]*>)?\s*\(", re.S
)
_PROVIDER_SUFFIX = "Provider"


def classify_class(extends: str | None, local_widgets: set[str]) -> str | None:
    if not extends:
        return None
    if extends.startswith("_$"):
        return "notifier"  # riverpod_generator base class
    if extends in FLUTTER_WIDGET_BASES or extends in local_widgets:
        return "widget"
    if extends in FLUTTER_STATE_BASES:
        return "state"
    return STATE_MANAGEMENT_BASES.get(extends)


def classify_top_level_initializer(text: str) -> str | None:
    match = _INITIALIZER_CTOR_RE.match(text)
    if not match:
        return None
    constructor = match.group(1)
    if constructor.endswith(_PROVIDER_SUFFIX):
        return "provider"
    if constructor == "GoRouter":
        return "router"
    return None


def provider_alias_candidates(callee: str) -> list[str]:
    """Declarations riverpod_generator may have generated ``callee`` from:
    ``fooProvider`` comes from function ``foo`` or class ``Foo`` (Riverpod 2
    and 3), or class ``FooNotifier`` (Riverpod 3 drops the suffix)."""
    if not callee.endswith(_PROVIDER_SUFFIX) or "." in callee:
        return []
    stem = callee[: -len(_PROVIDER_SUFFIX)]
    if not stem:
        return []
    upper = stem[:1].upper() + stem[1:]
    return [stem, upper, f"{upper}Notifier"]
