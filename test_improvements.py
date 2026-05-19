#!/usr/bin/env python3
"""Test script to verify duplicate detection and other improvements."""

from repo_scanner.github_client import _normalize_title, _titles_are_similar


def test_normalize_title():
    """Test title normalization."""
    assert (
        _normalize_title("[HIGH] Animation frame not cancelled")
        == "animation frame not cancelled"
    )
    assert _normalize_title("[MEDIUM] Missing validation") == "missing validation"
    assert _normalize_title("[CRITICAL] Security issue") == "security issue"
    assert _normalize_title("No severity prefix") == "no severity prefix"
    print("[OK] Title normalization tests passed")


def test_titles_are_similar():
    """Test title similarity detection."""
    # Exact match after normalization
    assert (
        _titles_are_similar(
            "[HIGH] Animation frame not cancelled on unmount",
            "[MEDIUM] Animation frame not cancelled on unmount",
        )
        == True
    )

    # Similar but not exact
    assert (
        _titles_are_similar(
            "[HIGH] Animation frame not cancelled on unmount",
            "[HIGH] requestAnimationFrame never cancelled on unmount",
        )
        == True
    )

    # Different issues
    assert (
        _titles_are_similar(
            "[HIGH] Animation frame not cancelled", "[MEDIUM] Missing validation"
        )
        == False
    )

    print("[OK] Title similarity tests passed")


def test_is_already_resolved_file_issue():
    """Test detection of already-resolved file issues."""
    from repo_scanner.fixer import _is_already_resolved_file_issue

    # Corrupted file issue
    issue1 = {
        "title": "[CRITICAL] Corrupted or malicious HTML file",
        "body": "The file contains binary/encoded data",
        "file": "temp.html",
    }
    assert _is_already_resolved_file_issue(issue1) == True

    # Regular file issue
    issue2 = {
        "title": "[HIGH] Missing null check",
        "body": "The function doesn't check for null",
        "file": "src/utils.js",
    }
    assert _is_already_resolved_file_issue(issue2) == False

    # Temporary file issue
    issue3 = {
        "title": "[MEDIUM] Temporary file issue",
        "body": "Some temporary file",
        "file": "temp.js",
    }
    assert _is_already_resolved_file_issue(issue3) == True

    print("[OK] Already-resolved file issue tests passed")


if __name__ == "__main__":
    test_normalize_title()
    test_titles_are_similar()
    test_is_already_resolved_file_issue()
    print("\n[SUCCESS] All tests passed!")
