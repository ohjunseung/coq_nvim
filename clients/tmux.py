from asyncio import Queue, as_completed, gather
from dataclasses import dataclass
from os import linesep
from shutil import which
from typing import AsyncIterator, Iterator, Sequence

from pynvim import Nvim

from .pkgs.da import call
from .pkgs.fc_types import Source, SourceCompletion, SourceFeed, SourceSeed
from .pkgs.nvim import print
from .pkgs.shared import coalesce


@dataclass(frozen=True)
class Config:
    min_length: int
    max_length: int


class TmuxError(Exception):
    pass


@dataclass(frozen=True)
class TmuxPane:
    session_id: str
    pane_id: str
    pane_active: bool
    window_active: bool


async def tmux_session() -> str:
    ret = await call("tmux", "display-message", "-p", "#{session_id}")
    if ret.code != 0:
        raise TmuxError(ret.err)
    else:
        return ret.out.strip()


async def tmux_panes() -> Sequence[TmuxPane]:
    ret = await call(
        "tmux",
        "list-panes",
        "-a",
        "-F",
        "#{session_id} #{pane_id} #{pane_active} #{window_active}",
    )

    def cont() -> Iterator[TmuxPane]:
        for line in ret.out.splitlines():
            session_id, pane_id, pane_active, window_active = line.split(" ")
            info = TmuxPane(
                session_id=session_id,
                pane_id=pane_id,
                pane_active=bool(int(pane_active)),
                window_active=bool(int(window_active)),
            )
            yield info

    if ret.code != 0:
        raise TmuxError(ret.err)
    else:
        return tuple(cont())


async def screenshot(pane: TmuxPane) -> str:
    ret = await call("tmux", "capture-pane", "-p", "-t", pane.pane_id)
    if ret.code != 0:
        raise TmuxError(ret.err)
    else:
        return ret.out


def is_active(session_id: str, pane: TmuxPane) -> bool:
    return session_id == pane.session_id and pane.pane_active and pane.window_active


async def main(nvim: Nvim, chan: Queue, seed: SourceSeed) -> Source:
    config = Config(**seed.config)

    async def source(feed: SourceFeed) -> AsyncIterator[SourceCompletion]:
        if which("tmux"):
            position = feed.position
            old_prefix = feed.context.alnums_before
            old_suffix = feed.context.alnums_after
            cword = feed.context.alnums_normalized

            parse = coalesce(
                cword=cword, min_length=config.min_length, max_length=config.max_length
            )
            try:
                session_id, panes = await gather(tmux_session(), tmux_panes())
                sources = tuple(
                    screenshot(pane)
                    for pane in panes
                    if not is_active(session_id, pane=pane)
                )

                for source in as_completed(sources):
                    text = await source
                    for word in parse(text):
                        yield SourceCompletion(
                            position=position,
                            old_prefix=old_prefix,
                            new_prefix=word,
                            old_suffix=old_suffix,
                            new_suffix="",
                        )
            except TmuxError as e:
                await print(nvim, f"tmux completion failed:{linesep}{e}", error=True)
        else:
            pass

    return source
