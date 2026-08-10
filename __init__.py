"""Hermes directory-plugin entrypoint."""

try:
    from .hermes_skill_publisher.plugin import register
except ImportError:  # direct source-tree import (for packaging/test discovery)
    from hermes_skill_publisher.plugin import register

__all__ = ["register"]
