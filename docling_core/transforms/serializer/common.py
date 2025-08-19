#
# Copyright IBM Corp. 2024 - 2025
# SPDX-License-Identifier: MIT
#

"""Define base classes for serialization."""
import re
import sys
from abc import abstractmethod
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple, Union

from pydantic import AnyUrl, BaseModel, ConfigDict, NonNegativeInt, computed_field
from typing_extensions import Self, override

from docling_core.transforms.serializer.base import (
    BaseAnnotationSerializer,
    BaseDocSerializer,
    BaseFallbackSerializer,
    BaseFormSerializer,
    BaseInlineSerializer,
    BaseKeyValueSerializer,
    BaseListSerializer,
    BasePictureSerializer,
    BaseTableSerializer,
    BaseTextSerializer,
    SerializationResult,
    Span,
)
from docling_core.types.doc.document import (
    DOCUMENT_TOKENS_EXPORT_LABELS,
    ContentLayer,
    DescriptionAnnotation,
    DocItem,
    DoclingDocument,
    FloatingItem,
    Formatting,
    FormItem,
    InlineGroup,
    KeyValueItem,
    ListGroup,
    NodeItem,
    PictureClassificationData,
    PictureDataType,
    PictureItem,
    PictureMoleculeData,
    Script,
    TableAnnotationType,
    TableItem,
    TextItem,
)
from docling_core.types.doc.labels import DocItemLabel

_DEFAULT_LABELS = DOCUMENT_TOKENS_EXPORT_LABELS
_DEFAULT_LAYERS = {cl for cl in ContentLayer}


class _PageBreakNode(NodeItem):
    """Page break node."""

    prev_page: int
    next_page: int


class _PageBreakSerResult(SerializationResult):
    """Page break serialization result."""

    node: _PageBreakNode


def _iterate_items(
    doc: DoclingDocument,
    layers: Optional[set[ContentLayer]],
    node: Optional[NodeItem] = None,
    traverse_pictures: bool = False,
    add_page_breaks: bool = False,
    visited: Optional[set[str]] = None,
):
    my_visited: set[str] = visited if visited is not None else set()
    prev_page_nr: Optional[int] = None
    page_break_i = 0
    
    # Get all page numbers that exist in the document, sorted
    existing_pages = sorted(doc.pages.keys()) if add_page_breaks else []
    
    for item, _ in doc.iterate_items(
        root=node,
        with_groups=True,
        included_content_layers=layers,
        traverse_pictures=traverse_pictures,
    ):
        if add_page_breaks:
            if (
                isinstance(item, (ListGroup, InlineGroup))
                and item.self_ref not in my_visited
            ):
                # if group starts with new page, yield page break before group node
                my_visited.add(item.self_ref)
                for it in _iterate_items(
                    doc=doc,
                    layers=layers,
                    node=item,
                    traverse_pictures=traverse_pictures,
                    add_page_breaks=add_page_breaks,
                    visited=my_visited,
                ):
                    if isinstance(it, DocItem) and it.prov:
                        page_no = it.prov[0].page_no
                        if prev_page_nr is not None and page_no > prev_page_nr:
                            # Generate page breaks for all existing pages between prev_page_nr and page_no
                            prev_page_idx = existing_pages.index(prev_page_nr) if prev_page_nr in existing_pages else -1
                            curr_page_idx = existing_pages.index(page_no) if page_no in existing_pages else -1
                            
                            if prev_page_idx >= 0 and curr_page_idx >= 0:
                                for i in range(prev_page_idx + 1, curr_page_idx + 1):
                                    target_page = existing_pages[i]
                                    prev_target_page = existing_pages[i-1] if i > 0 else prev_page_nr
                                    yield _PageBreakNode(
                                        self_ref=f"#/pb/{page_break_i}",
                                        prev_page=prev_target_page,
                                        next_page=target_page,
                                    )
                                    page_break_i += 1
                        break
            elif isinstance(item, DocItem) and item.prov:
                page_no = item.prov[0].page_no
                if prev_page_nr is None or page_no > prev_page_nr:
                    if prev_page_nr is not None:  # close previous range
                        # Generate page breaks for all existing pages between prev_page_nr and page_no
                        prev_page_idx = existing_pages.index(prev_page_nr) if prev_page_nr in existing_pages else -1
                        curr_page_idx = existing_pages.index(page_no) if page_no in existing_pages else -1
                        
                        if prev_page_idx >= 0 and curr_page_idx >= 0:
                            for i in range(prev_page_idx + 1, curr_page_idx + 1):
                                target_page = existing_pages[i]
                                prev_target_page = existing_pages[i-1] if i > 0 else prev_page_nr
                                yield _PageBreakNode(
                                    self_ref=f"#/pb/{page_break_i}",
                                    prev_page=prev_target_page,
                                    next_page=target_page,
                                )
                                page_break_i += 1
                    prev_page_nr = page_no
        yield item


def _get_annotation_text(
    annotation: Union[PictureDataType, TableAnnotationType],
) -> Optional[str]:
    result = None
    if isinstance(annotation, PictureClassificationData):
        predicted_class = (
            annotation.predicted_classes[0].class_name
            if annotation.predicted_classes
            else None
        )
        if predicted_class is not None:
            result = predicted_class.replace("_", " ")
    elif isinstance(annotation, DescriptionAnnotation):
        result = annotation.text
    elif isinstance(annotation, PictureMoleculeData):
        result = annotation.smi
    return result


def create_ser_result(
    *,
    text: str = "",
    span_source: Union[DocItem, list[SerializationResult]] = [],
) -> SerializationResult:
    """Function for creating `SerializationResult` instances.

    Args:
        text: the text the use. Defaults to "".
        span_source: the item or list of results to use as span source. Defaults to [].

    Returns:
        The created `SerializationResult`.
    """
    spans: list[Span]
    if isinstance(span_source, DocItem):
        spans = [Span(item=span_source)]
    else:
        results: list[SerializationResult] = span_source
        spans = []
        span_ids: set[str] = set()
        for ser_res in results:
            for span in ser_res.spans:
                if (span_id := span.item.self_ref) not in span_ids:
                    span_ids.add(span_id)
                    spans.append(span)
    return SerializationResult(
        text=text,
        spans=spans,
    )


class CommonParams(BaseModel):
    """Common serialization parameters."""

    # allowlists with non-recursive semantics, i.e. if a list group node is outside the
    # range and some of its children items are within, they will be serialized
    labels: set[DocItemLabel] = _DEFAULT_LABELS
    layers: set[ContentLayer] = _DEFAULT_LAYERS
    pages: Optional[set[int]] = None  # None means all pages are allowed

    # slice-like semantics: start is included, stop is excluded
    start_idx: NonNegativeInt = 0
    stop_idx: NonNegativeInt = sys.maxsize

    include_formatting: bool = True
    include_hyperlinks: bool = True
    caption_delim: str = " "

    def merge_with_patch(self, patch: dict[str, Any]) -> Self:
        """Create an instance by merging the provided patch dict on top of self."""
        res = self.model_copy(update=patch)
        return res


class DocSerializer(BaseModel, BaseDocSerializer):
    """Class for document serializers."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    doc: DoclingDocument

    text_serializer: BaseTextSerializer
    table_serializer: BaseTableSerializer
    picture_serializer: BasePictureSerializer
    key_value_serializer: BaseKeyValueSerializer
    form_serializer: BaseFormSerializer
    fallback_serializer: BaseFallbackSerializer

    list_serializer: BaseListSerializer
    inline_serializer: BaseInlineSerializer

    annotation_serializer: BaseAnnotationSerializer

    params: CommonParams = CommonParams()

    _excluded_refs_cache: dict[str, set[str]] = {}

    @computed_field  # type: ignore[misc]
    @cached_property
    def _captions_of_some_item(self) -> set[str]:
        layers = {cl for cl in ContentLayer}  # TODO review
        refs = {
            cap.cref
            for (item, _) in self.doc.iterate_items(
                with_groups=True,
                traverse_pictures=True,
                included_content_layers=layers,
            )
            for cap in (item.captions if isinstance(item, FloatingItem) else [])
        }
        return refs

    @override
    def get_excluded_refs(self, **kwargs: Any) -> set[str]:
        """References to excluded items."""
        params = self.params.merge_with_patch(patch=kwargs)
        params_json = params.model_dump_json()
        refs = self._excluded_refs_cache.get(params_json)
        if refs is None:
            refs = {
                item.self_ref
                for ix, item in enumerate(
                    _iterate_items(
                        doc=self.doc,
                        traverse_pictures=True,
                        layers=params.layers,
                    )
                )
                if (
                    (ix < params.start_idx or ix >= params.stop_idx)
                    or (
                        isinstance(item, DocItem)
                        and (
                            item.label not in params.labels
                            or item.content_layer not in params.layers
                            or (
                                params.pages is not None
                                and (
                                    (not item.prov)
                                    or item.prov[0].page_no not in params.pages
                                )
                            )
                        )
                    )
                )
            }
            self._excluded_refs_cache[params_json] = refs
        return refs

    @abstractmethod
    def serialize_doc(
        self,
        *,
        parts: list[SerializationResult],
        **kwargs: Any,
    ) -> SerializationResult:
        """Serialize a document out of its pages."""
        ...

    def _serialize_body(self, **kwargs) -> SerializationResult:
        """Serialize the document body."""
        subparts = self.get_parts(**kwargs)
        res = self.serialize_doc(parts=subparts, **kwargs)
        return res

    @override
    def serialize(
        self,
        *,
        item: Optional[NodeItem] = None,
        list_level: int = 0,
        is_inline_scope: bool = False,
        visited: Optional[set[str]] = None,  # refs of visited items
        **kwargs: Any,
    ) -> SerializationResult:
        """Serialize a given node."""
        my_visited: set[str] = visited if visited is not None else set()
        my_kwargs = {**self.params.model_dump(), **kwargs}
        empty_res = create_ser_result()
        if item is None or item == self.doc.body:
            if self.doc.body.self_ref not in my_visited:
                my_visited.add(self.doc.body.self_ref)
                return self._serialize_body(**my_kwargs)
            else:
                return empty_res

        my_visited.add(item.self_ref)

        ########
        # groups
        ########
        if isinstance(item, ListGroup):
            part = self.list_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                list_level=list_level,
                is_inline_scope=is_inline_scope,
                visited=my_visited,
                **my_kwargs,
            )
        elif isinstance(item, InlineGroup):
            part = self.inline_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                list_level=list_level,
                visited=my_visited,
                **my_kwargs,
            )
        ###########
        # doc items
        ###########
        elif isinstance(item, TextItem):
            if item.self_ref in self._captions_of_some_item:
                # those captions will be handled by the floating item holding them
                return empty_res
            else:
                part = (
                    self.text_serializer.serialize(
                        item=item,
                        doc_serializer=self,
                        doc=self.doc,
                        is_inline_scope=is_inline_scope,
                        visited=my_visited,
                        **my_kwargs,
                    )
                    if item.self_ref not in self.get_excluded_refs(**kwargs)
                    else empty_res
                )
        elif isinstance(item, TableItem):
            part = self.table_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                **my_kwargs,
            )
        elif isinstance(item, PictureItem):
            part = self.picture_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                visited=my_visited,
                **my_kwargs,
            )
        elif isinstance(item, KeyValueItem):
            part = self.key_value_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                **my_kwargs,
            )
        elif isinstance(item, FormItem):
            part = self.form_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                **my_kwargs,
            )
        elif isinstance(item, _PageBreakNode):
            part = _PageBreakSerResult(
                text=self._create_page_break(node=item),
                node=item,
            )
        else:
            part = self.fallback_serializer.serialize(
                item=item,
                doc_serializer=self,
                doc=self.doc,
                **my_kwargs,
            )
        return part

    # making some assumptions about the kwargs it can pass
    @override
    def get_parts(
        self,
        item: Optional[NodeItem] = None,
        *,
        traverse_pictures: bool = False,
        list_level: int = 0,
        is_inline_scope: bool = False,
        visited: Optional[set[str]] = None,  # refs of visited items
        **kwargs: Any,
    ) -> list[SerializationResult]:
        """Get the components to be combined for serializing this node."""
        parts: list[SerializationResult] = []
        my_visited: set[str] = visited if visited is not None else set()
        params = self.params.merge_with_patch(patch=kwargs)

        for node in _iterate_items(
            node=item,
            doc=self.doc,
            layers=params.layers,
            add_page_breaks=self.requires_page_break(),
        ):
            if node.self_ref in my_visited:
                continue
            else:
                my_visited.add(node.self_ref)
            part = self.serialize(
                item=node,
                list_level=list_level,
                is_inline_scope=is_inline_scope,
                visited=my_visited,
                **kwargs,
            )
            if part.text:
                parts.append(part)
        return parts

    @override
    def post_process(
        self,
        text: str,
        *,
        formatting: Optional[Formatting] = None,
        hyperlink: Optional[Union[AnyUrl, Path]] = None,
        **kwargs: Any,
    ) -> str:
        """Apply some text post-processing steps."""
        params = self.params.merge_with_patch(patch=kwargs)
        res = text
        if params.include_formatting and formatting:
            if formatting.bold:
                res = self.serialize_bold(text=res)
            if formatting.italic:
                res = self.serialize_italic(text=res)
            if formatting.underline:
                res = self.serialize_underline(text=res)
            if formatting.strikethrough:
                res = self.serialize_strikethrough(text=res)
            if formatting.script == Script.SUB:
                res = self.serialize_subscript(text=res)
            elif formatting.script == Script.SUPER:
                res = self.serialize_superscript(text=res)
        if params.include_hyperlinks and hyperlink:
            res = self.serialize_hyperlink(text=res, hyperlink=hyperlink)
        return res

    @override
    def serialize_bold(self, text: str, **kwargs: Any) -> str:
        """Hook for bold formatting serialization."""
        return text

    @override
    def serialize_italic(self, text: str, **kwargs: Any) -> str:
        """Hook for italic formatting serialization."""
        return text

    @override
    def serialize_underline(self, text: str, **kwargs: Any) -> str:
        """Hook for underline formatting serialization."""
        return text

    @override
    def serialize_strikethrough(self, text: str, **kwargs: Any) -> str:
        """Hook for strikethrough formatting serialization."""
        return text

    @override
    def serialize_subscript(self, text: str, **kwargs: Any) -> str:
        """Hook for subscript formatting serialization."""
        return text

    @override
    def serialize_superscript(self, text: str, **kwargs: Any) -> str:
        """Hook for superscript formatting serialization."""
        return text

    @override
    def serialize_hyperlink(
        self,
        text: str,
        hyperlink: Union[AnyUrl, Path],
        **kwargs: Any,
    ) -> str:
        """Hook for hyperlink serialization."""
        return text

    @override
    def serialize_captions(
        self,
        item: FloatingItem,
        **kwargs: Any,
    ) -> SerializationResult:
        """Serialize the item's captions."""
        params = self.params.merge_with_patch(patch=kwargs)
        results: list[SerializationResult] = []
        if DocItemLabel.CAPTION in params.labels:
            results = [
                create_ser_result(text=it.text, span_source=it)
                for cap in item.captions
                if isinstance(it := cap.resolve(self.doc), TextItem)
                and it.self_ref not in self.get_excluded_refs(**kwargs)
            ]
            text_res = params.caption_delim.join([r.text for r in results])
            text_res = self.post_process(text=text_res)
        else:
            text_res = ""
        return create_ser_result(text=text_res, span_source=results)

    @override
    def serialize_annotations(
        self,
        item: DocItem,
        **kwargs: Any,
    ) -> SerializationResult:
        """Serialize the item's annotations."""
        return self.annotation_serializer.serialize(
            item=item,
            doc=self.doc,
            **kwargs,
        )

    def _get_applicable_pages(self) -> Optional[list[int]]:
        pages = {
            item.prov[0].page_no: ...
            for ix, (item, _) in enumerate(
                self.doc.iterate_items(
                    with_groups=True,
                    included_content_layers=self.params.layers,
                    traverse_pictures=True,
                )
            )
            if (
                isinstance(item, DocItem)
                and item.prov
                and (
                    self.params.pages is None
                    or item.prov[0].page_no in self.params.pages
                )
                and ix >= self.params.start_idx
                and ix < self.params.stop_idx
            )
        }
        return [p for p in pages] or None

    def _create_page_break(self, node: _PageBreakNode) -> str:
        return f"#_#_DOCLING_DOC_PAGE_BREAK_{node.prev_page}_{node.next_page}_#_#"

    def _get_page_breaks(self, text: str) -> Iterable[Tuple[str, int, int]]:
        pattern = r"#_#_DOCLING_DOC_PAGE_BREAK_(\d+)_(\d+)_#_#"
        matches = re.finditer(pattern, text)
        for match in matches:
            full_match = match.group(0)
            prev_page_nr = int(match.group(1))
            next_page_nr = int(match.group(2))
            yield (full_match, prev_page_nr, next_page_nr)
