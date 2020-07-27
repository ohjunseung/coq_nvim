from dataclasses import dataclass
from itertools import chain, count
from os import linesep
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union, cast

from pynvim import Nvim
from pynvim.api.buffer import Buffer
from pynvim.api.window import Window

from .types import LEdit, Payload, Position


@dataclass(frozen=True)
class Replacement:
    begin: int
    length: int
    text: str
    cursor: bool = False


# 0 based
def rows_to_fetch(payload: Payload) -> Tuple[int, int]:
    row = payload.position.row
    edits = payload.ledits
    old_lc, new_lc = (
        payload.old_prefix.count(linesep),
        payload.old_suffix.count(linesep),
    )
    main_btm, main_top = row - old_lc, row + new_lc
    btm_idx = min(chain((e.begin.row for e in edits), (main_btm,)))
    top_idx = max(chain((e.end.row for e in edits), (main_top,)))
    return btm_idx, top_idx


def row_lengths(rows: Sequence[str], start: int) -> Dict[int, int]:
    ret = {idx: len(row) + 1 for idx, row in enumerate(rows, start)}
    return ret


def calculate_replacement(
    row_lens: Dict[int, int], start: int, edit: LEdit
) -> Replacement:
    b_row, e_row = edit.begin.row, edit.end.row
    b_col, e_col = edit.begin.col, edit.end.col

    lower = sum(row_lens[r] for r in range(start, b_row)) + b_col
    begin = row_lens[b_row] - b_col - 1
    middle = sum(row_lens[r] for r in range(b_row + 1, e_row))
    end = e_col
    length = begin + middle + end
    text = edit.new_text

    replacement = Replacement(begin=lower, length=length, text=text)
    return replacement


def calculate_main_replacements(
    row_lens: Dict[int, int], start: int, payload: Payload
) -> Tuple[Replacement, Replacement]:
    row, col = payload.position.row, payload.position.col

    length1 = len(payload.old_prefix)
    begin1 = sum(row_lens[r] for r in range(start, row)) + col - length1
    length2 = len(payload.old_suffix)
    begin2 = col

    replacement1 = Replacement(
        begin=begin1, length=length1, text=payload.new_prefix, cursor=True
    )
    replacement2 = Replacement(begin=begin2, length=length2, text=payload.new_suffix)
    return replacement1, replacement2


def overlap(r1: Replacement, r2: Replacement) -> bool:
    r1_end, r2_end = r1.begin + r1.length, r2.begin + r2.length
    return bool(range(max(r1.begin, r2.begin), min(r1_end, r2_end) + 1))


def rank(replacement: Replacement) -> Tuple[int, int, str]:
    return replacement.begin, replacement.length, replacement.text


def consolidate_replacements(
    row_lens: Dict[int, int], start: int, payload: Payload
) -> Sequence[Replacement]:
    main_replacements = calculate_main_replacements(
        row_lens, start=start, payload=payload
    )
    auxiliary_replacements = (
        calculate_replacement(row_lens, start=start, edit=edit)
        for edit in payload.ledits
    )

    def cont() -> Iterator[Replacement]:
        seen: List[Replacement] = [*main_replacements]
        yield from iter(main_replacements)
        for r in sorted(auxiliary_replacements, key=rank):
            if not any(overlap(r, s) for s in seen):
                seen.append(r)
                yield r

    return sorted(cont(), key=rank)


def stream_lines(rows: Sequence[str]) -> Iterator[Tuple[int, str]]:
    it = count()
    for row in rows:
        for char in row:
            yield next(it), char
        yield next(it), linesep


def perform_edits(
    stream: Iterator[Tuple[int, str]], replacements: Iterator[Replacement]
) -> Iterator[Union[str, Tuple[()]]]:
    replacement = next(replacements, None)
    for idx, char in stream:
        if replacement and idx == replacement.begin:
            yield from iter(replacement.text)
            if replacement.cursor:
                yield ()
            for _ in zip(stream, range(replacement.length)):
                pass
            replacement = next(replacements, None)
        else:
            yield char


def split_stream(
    stream: Iterator[Union[str, Tuple[()]]], start: int
) -> Tuple[Sequence[str], Position]:
    position: Optional[Position] = None

    def cont() -> Iterator[str]:
        nonlocal position
        curr: List[str] = []
        r_it, c_it = count(start), count()
        r, c = next(r_it), next(c_it)

        for char in stream:
            if char == ():
                position = Position(row=r, col=c)
            elif char == linesep:
                yield "".join(curr)
                curr.clear()
                r = next(r_it)
                c_it = count()
                c = 0
            else:
                c = next(c_it)
                curr.append(cast(str, char))
        if curr:
            yield "".join(curr)

    return tuple(cont()), cast(Position, position)


def replace_lines(nvim: Nvim, payload: Payload) -> None:
    btm_idx, top_idx = rows_to_fetch(payload)
    top_idx = top_idx + 1

    win: Window = nvim.api.get_current_win()
    buf: Buffer = nvim.api.get_current_buf()
    old_lines: Sequence[str] = nvim.api.buf_get_lines(buf, btm_idx, top_idx, True)

    row_lens = row_lengths(old_lines, start=btm_idx)
    replacements = consolidate_replacements(row_lens, start=btm_idx, payload=payload)
    stream = stream_lines(old_lines)
    text_stream = perform_edits(stream, replacements=iter(replacements))
    new_lines, pos = split_stream(text_stream, start=btm_idx)

    nvim.api.buf_set_lines(buf, btm_idx, top_idx, True, new_lines)
    nvim.api.win_set_cursor(win, (pos.row + 1, pos.col))

    nvim.api.out_write(str(payload) + "\n")
    nvim.api.out_write(str(old_lines) + "\n")
    nvim.api.out_write(str(replacements) + "\n")
    nvim.api.out_write(str(pos) + "\n")


def apply_patch(nvim: Nvim, comp: Dict[str, Any]) -> None:
    data = comp.get("user_data")
    d = cast(dict, data)
    try:
        position = Position(**d["position"])
        edits = tuple(
            LEdit(
                begin=Position(row=edit["begin"]["row"], col=edit["begin"]["col"]),
                end=Position(row=edit["end"]["row"], col=edit["end"]["col"]),
                new_text=edit["new_text"],
            )
            for edit in d["ledits"]
        )
        payload = Payload(**{**d, **dict(position=position, ledits=edits)})
    except (KeyError, TypeError):
        pass
    else:
        replace_lines(nvim, payload=payload)
