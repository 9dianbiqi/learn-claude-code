"""Deterministic advice for trusted, typed read failures. Never retries tools."""
from dataclasses import dataclass
from pathlib import PurePosixPath


class RecoverableReadError(FileNotFoundError):
    """Trusted remote read adapter's structured PATH_NOT_FOUND signal.

    A Shell stderr string is not sufficient to construct this signal. The
    adapter must have confirmed a missing repository-relative path itself.
    """
    def __init__(self, path):
        super().__init__(f"Repository path not found: {path}")
        self.repository_path = path


@dataclass(frozen=True)
class RecoveryAdvisor:
    max_advice_paths: int = 2
    version: str = "1"

    def __post_init__(self):
        if type(self.max_advice_paths) is not int or not 1 <= self.max_advice_paths <= 3:
            raise ValueError("max_advice_paths must be between 1 and 3")
        if self.version != "1":
            raise ValueError("Unsupported advisor version")

    def manifest(self):
        return {"version": self.version, "max_advice_paths": self.max_advice_paths}

    def advice(self, path, prior_paths):
        path = str(path).replace("\\", "/")
        if (not path or path.startswith("/") or ":" in path
                or ".." in PurePosixPath(path).parts):
            raise ValueError("Advice requires a repository-relative path")
        path = PurePosixPath(path).as_posix()
        first = path not in prior_paths and len(prior_paths) < self.max_advice_paths
        return {"error_type": "PATH_NOT_FOUND", "failed_path": path,
                "same_arguments_retryable": False, "task_recoverable": True,
                "repeated_path": path in prior_paths,
                "suggested_actions": ["inspect_repository_structure", "search_files_by_name",
                                      "search_symbols"] if first else [],
                "advice_emitted": first, "advisor_version": self.version}
