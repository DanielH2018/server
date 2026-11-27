"""The pod-override models the DAGs' `executor_config` builds; the real client is only in the image."""

class V1SecretKeySelector:
    def __init__(self, *, name: str, key: str) -> None: ...

class V1EnvVarSource:
    def __init__(self, *, secret_key_ref: V1SecretKeySelector) -> None: ...

class V1EnvVar:
    def __init__(self, *, name: str, value_from: V1EnvVarSource) -> None: ...

class V1Container:
    def __init__(self, *, name: str, env: list[V1EnvVar] = ...) -> None: ...

class V1PodSpec:
    def __init__(
        self,
        *,
        containers: list[V1Container],
        service_account_name: str = ...,
        automount_service_account_token: bool = ...,
    ) -> None: ...

class V1Pod:
    def __init__(self, *, spec: V1PodSpec) -> None: ...
