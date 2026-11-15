"""Per-language tree-sitter specifications used by the indexer.

Each LangSpec tells the generic tree walker which node types are definitions,
calls, imports, decorators and branches, so one walker serves every language.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

IDENT_TYPES = frozenset({
    "identifier", "field_identifier", "property_identifier", "type_identifier",
    "simple_identifier", "name", "constant", "namespace_identifier",
    "shorthand_property_identifier", "statement_identifier", "word",
    "variable_name", "private_property_identifier",
})

COMMENT_TYPES = frozenset({"comment", "line_comment", "block_comment", "doc_comment"})

STRING_TYPES = frozenset({"string", "string_literal", "interpreted_string_literal", "raw_string_literal",
                          "string_fragment", "string_content", "template_string", "encapsed_string",
                          "line_string_literal", "string_scalar"})


@dataclass(frozen=True)
class LangSpec:
    name: str
    extensions: tuple[str, ...]
    # node_type -> symbol kind
    definitions: dict[str, str] = field(default_factory=dict)
    # node_type -> field holding the name (defaults to "name")
    name_field: dict[str, str] = field(default_factory=dict)
    # node types whose kind depends on the value they hold (JS: const f = () => {})
    value_defs: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    # node_type -> field holding the callee expression ("__first" = first named child)
    calls: dict[str, str] = field(default_factory=dict)
    imports: tuple[str, ...] = ()
    decorators: tuple[str, ...] = ()
    branches: frozenset[str] = frozenset()
    catch_clause: tuple[str, ...] = ()
    bare_except: tuple[str, ...] = ()
    export_wrappers: tuple[str, ...] = ()


CLASS_KINDS = frozenset({"class", "struct", "interface", "trait", "impl", "enum", "record", "module", "object"})

_PY_BRANCH = frozenset({"if_statement", "elif_clause", "for_statement", "while_statement", "except_clause",
                        "with_statement", "boolean_operator", "conditional_expression", "match_statement",
                        "case_clause", "list_comprehension", "assert_statement"})
_C_BRANCH = frozenset({"if_statement", "for_statement", "while_statement", "do_statement", "case_statement",
                       "switch_statement", "catch_clause", "conditional_expression", "ternary_expression",
                       "for_in_statement", "for_range_loop", "for_each_statement",
                       "foreach_statement", "when_entry", "guard_statement", "match_arm", "match_expression",
                       "if_expression", "while_expression", "loop_expression", "for_expression",
                       "if_let_expression", "catch_block", "else_if_clause", "elseif_clause", "case_clause",
                       "switch_case", "try_expression", "when", "unless", "until", "rescue", "elsif",
                       "logical_expression", "and_expression", "or_expression"})

SPECS: dict[str, LangSpec] = {}


def _reg(spec: LangSpec) -> None:
    SPECS[spec.name] = spec


_reg(LangSpec(
    name="python", extensions=(".py", ".pyi"),
    definitions={"function_definition": "function", "class_definition": "class"},
    calls={"call": "function"},
    imports=("import_statement", "import_from_statement", "future_import_statement"),
    decorators=("decorator",),
    branches=_PY_BRANCH,
    bare_except=("except_clause",),
))

_JS_DEFS = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "method_definition": "method", "class_declaration": "class",
}
_JS_VALUE = {
    "variable_declarator": ("value", {"arrow_function": "function", "function_expression": "function",
                                      "function": "function", "generator_function": "function", "class": "class"}),
    "pair": ("value", {"arrow_function": "function", "function_expression": "function", "function": "function"}),
    "public_field_definition": ("value", {"arrow_function": "method", "function_expression": "method"}),
    "field_definition": ("value", {"arrow_function": "method", "function_expression": "method"}),
}
_JS_CALLS = {"call_expression": "function", "new_expression": "constructor"}
_JS_BRANCH = _C_BRANCH | frozenset({"for_of_statement", "for_in_statement", "ternary_expression"})

_reg(LangSpec(
    name="javascript", extensions=(".js", ".jsx", ".mjs", ".cjs"),
    definitions=_JS_DEFS, value_defs=_JS_VALUE, name_field={"pair": "key", "field_definition": "property"},
    calls=_JS_CALLS, imports=("import_statement",), decorators=("decorator",),
    branches=_JS_BRANCH, catch_clause=("catch_clause",), export_wrappers=("export_statement",),
))
_TS_DEFS = dict(_JS_DEFS, **{
    "interface_declaration": "interface", "type_alias_declaration": "type", "enum_declaration": "enum",
    "abstract_class_declaration": "class", "method_signature": "method", "abstract_method_signature": "method",
    "function_signature": "function", "internal_module": "module",
})
_reg(LangSpec(
    name="typescript", extensions=(".ts", ".mts", ".cts"),
    definitions=_TS_DEFS, value_defs=_JS_VALUE, name_field={"pair": "key", "field_definition": "property"},
    calls=_JS_CALLS, imports=("import_statement",), decorators=("decorator",),
    branches=_JS_BRANCH, catch_clause=("catch_clause",), export_wrappers=("export_statement",),
))
_reg(LangSpec(
    name="tsx", extensions=(".tsx",),
    definitions=_TS_DEFS, value_defs=_JS_VALUE, name_field={"pair": "key", "field_definition": "property"},
    calls=_JS_CALLS, imports=("import_statement",), decorators=("decorator",),
    branches=_JS_BRANCH, catch_clause=("catch_clause",), export_wrappers=("export_statement",),
))

_reg(LangSpec(
    name="go", extensions=(".go",),
    definitions={"function_declaration": "function", "method_declaration": "method", "type_spec": "type"},
    calls={"call_expression": "function"},
    imports=("import_spec",),
    branches=_C_BRANCH | frozenset({"expression_case", "type_case", "communication_case"}),
))

_reg(LangSpec(
    name="rust", extensions=(".rs",),
    definitions={"function_item": "function", "function_signature_item": "function", "struct_item": "struct",
                 "enum_item": "enum", "trait_item": "trait", "impl_item": "impl", "mod_item": "module",
                 "type_item": "type", "const_item": "constant", "static_item": "constant",
                 "macro_definition": "macro", "union_item": "struct"},
    name_field={"impl_item": "type"},
    calls={"call_expression": "function", "macro_invocation": "macro"},
    imports=("use_declaration",),
    decorators=("attribute_item",),
    branches=_C_BRANCH,
))

_reg(LangSpec(
    name="java", extensions=(".java",),
    definitions={"method_declaration": "method", "constructor_declaration": "method", "class_declaration": "class",
                 "interface_declaration": "interface", "enum_declaration": "enum", "record_declaration": "record",
                 "annotation_type_declaration": "interface"},
    calls={"method_invocation": "name", "object_creation_expression": "type"},
    imports=("import_declaration",),
    decorators=("annotation", "marker_annotation"),
    branches=_C_BRANCH | frozenset({"enhanced_for_statement", "switch_block_statement_group", "switch_rule"}),
    catch_clause=("catch_clause",),
))

_reg(LangSpec(
    name="kotlin", extensions=(".kt", ".kts"),
    definitions={"function_declaration": "function", "class_declaration": "class", "object_declaration": "object"},
    calls={"call_expression": "__first"},
    imports=("import_header",),
    decorators=("annotation",),
    branches=_C_BRANCH | frozenset({"when_expression", "catch_block"}),
    catch_clause=("catch_block",),
))

_reg(LangSpec(
    name="swift", extensions=(".swift",),
    definitions={"function_declaration": "function", "class_declaration": "class",
                 "protocol_declaration": "interface", "init_declaration": "method",
                 "protocol_function_declaration": "method"},
    calls={"call_expression": "__first"},
    imports=("import_declaration",),
    decorators=("attribute",),
    branches=_C_BRANCH | frozenset({"switch_entry", "repeat_while_statement"}),
    catch_clause=("catch_block",),
))

_C_DEFS = {"function_definition": "function", "struct_specifier": "struct", "union_specifier": "struct",
           "enum_specifier": "enum", "type_definition": "type", "preproc_function_def": "macro",
           "preproc_def": "macro"}
_reg(LangSpec(
    name="c", extensions=(".c", ".h"),
    definitions=_C_DEFS, name_field={"function_definition": "declarator", "type_definition": "declarator"},
    calls={"call_expression": "function"},
    imports=("preproc_include",),
    branches=_C_BRANCH,
))
_reg(LangSpec(
    name="cpp", extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ipp", ".cu"),
    definitions=dict(_C_DEFS, **{"class_specifier": "class", "namespace_definition": "module",
                                 "alias_declaration": "type", "concept_definition": "type"}),
    name_field={"function_definition": "declarator", "type_definition": "declarator"},
    calls={"call_expression": "function", "new_expression": "type"},
    imports=("preproc_include",),
    branches=_C_BRANCH,
    catch_clause=("catch_clause",),
))

_reg(LangSpec(
    name="csharp", extensions=(".cs",),
    definitions={"method_declaration": "method", "constructor_declaration": "method", "class_declaration": "class",
                 "interface_declaration": "interface", "struct_declaration": "struct", "enum_declaration": "enum",
                 "record_declaration": "record", "property_declaration": "property",
                 "delegate_declaration": "type", "namespace_declaration": "module",
                 "file_scoped_namespace_declaration": "module", "local_function_statement": "function"},
    calls={"invocation_expression": "function", "object_creation_expression": "type"},
    imports=("using_directive",),
    decorators=("attribute_list",),
    branches=_C_BRANCH | frozenset({"switch_section", "switch_expression_arm"}),
    catch_clause=("catch_clause",),
))

_reg(LangSpec(
    name="ruby", extensions=(".rb", ".rake", ".gemspec"),
    definitions={"method": "method", "singleton_method": "method", "class": "class", "module": "module"},
    calls={"call": "method"},
    imports=(),  # require / require_relative are calls; handled specially in the walker
    branches=_C_BRANCH | frozenset({"if", "unless", "while", "until", "for", "case", "when", "rescue", "elsif",
                                     "conditional", "if_modifier", "unless_modifier", "while_modifier"}),
))

_reg(LangSpec(
    name="php", extensions=(".php",),
    definitions={"function_definition": "function", "method_declaration": "method", "class_declaration": "class",
                 "interface_declaration": "interface", "trait_declaration": "trait", "enum_declaration": "enum"},
    calls={"function_call_expression": "function", "member_call_expression": "name",
           "scoped_call_expression": "name", "object_creation_expression": "__first",
           "nullsafe_member_call_expression": "name"},
    imports=("namespace_use_declaration", "require_expression", "require_once_expression",
             "include_expression", "include_once_expression"),
    decorators=("attribute_list",),
    branches=_C_BRANCH | frozenset({"match_conditional_expression"}),
    catch_clause=("catch_clause",),
))

_reg(LangSpec(
    name="lua", extensions=(".lua",),
    definitions={"function_declaration": "function"},
    calls={"function_call": "name"},
    branches=_C_BRANCH | frozenset({"elseif_statement", "repeat_statement"}),
))

_reg(LangSpec(
    name="bash", extensions=(".sh", ".bash", ".zsh"),
    definitions={"function_definition": "function"},
    calls={"command": "name"},
    branches=_C_BRANCH | frozenset({"elif_clause", "case_item", "until_statement"}),
))

_reg(LangSpec(
    name="scala", extensions=(".scala", ".sc"),
    definitions={"function_definition": "function", "class_definition": "class", "object_definition": "object",
                 "trait_definition": "trait", "function_declaration": "function"},
    calls={"call_expression": "function"},
    imports=("import_declaration",),
    branches=_C_BRANCH,
))

EXT_TO_LANG: dict[str, str] = {}
for _spec in SPECS.values():
    for _ext in _spec.extensions:
        EXT_TO_LANG.setdefault(_ext, _spec.name)


def lang_for_path(path: str) -> Optional[str]:
    lower = path.lower()
    dot = lower.rfind(".")
    if dot == -1:
        return None
    return EXT_TO_LANG.get(lower[dot:])


@lru_cache(maxsize=None)
def get_parser(lang: str):
    from tree_sitter_language_pack import get_parser as _gp
    return _gp(lang)  # type: ignore[arg-type]


def supported_languages() -> list[str]:
    return sorted(SPECS)
