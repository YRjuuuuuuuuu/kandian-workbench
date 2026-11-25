"""Preserve model prose and private provenance without injecting media prefixes."""
import copy


def complete(output, facts):
    return copy.deepcopy(output)
