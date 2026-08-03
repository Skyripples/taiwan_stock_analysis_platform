"""Automatic discovery registry for market data providers."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Dict, List, Type

from .base_provider import BaseProvider


class ProviderRegistry:
    """Discover and instantiate provider classes in the providers package."""

    def __init__(self) -> None:
        self._provider_classes: Dict[str, Type[BaseProvider]] = {}
        self._discovered = False

    def register(self, provider_class: Type[BaseProvider]) -> None:
        """Register one provider class using its provider and dataset identity."""

        if not issubclass(provider_class, BaseProvider):
            raise TypeError("Provider must inherit from BaseProvider")
        provider_name = str(provider_class.name).strip()
        if not provider_name or provider_name == BaseProvider.name:
            raise ValueError("Provider must define a unique non-empty name")
        dataset = str(provider_class.dataset).strip()
        registry_key = str(
            getattr(provider_class, "registry_name", "") or f"{provider_name}:{dataset}"
        ).strip()
        existing = self._provider_classes.get(registry_key)
        if existing is not None and existing is not provider_class:
            raise ValueError(f"Duplicate provider registry key: {registry_key}")
        self._provider_classes[registry_key] = provider_class

    def discover(self, package_name: str = "providers") -> None:
        """Import provider modules and register every concrete provider class."""

        if self._discovered:
            return

        package = importlib.import_module(package_name)
        module_names = sorted(
            module_info.name
            for module_info in pkgutil.iter_modules(package.__path__)
            if module_info.name.endswith("_provider") and module_info.name != "base_provider"
        )

        for module_name in module_names:
            module = importlib.import_module(f"{package_name}.{module_name}")
            for _, provider_class in inspect.getmembers(module, inspect.isclass):
                if (
                    provider_class is not BaseProvider
                    and issubclass(provider_class, BaseProvider)
                    and provider_class.__module__ == module.__name__
                    and not inspect.isabstract(provider_class)
                ):
                    self.register(provider_class)

        self._discovered = True

    def create_providers(self) -> List[BaseProvider]:
        """Return a new instance of each provider in deterministic order."""

        return [
            self._provider_classes[name]()
            for name in sorted(self._provider_classes, key=str.casefold)
        ]

    @property
    def count(self) -> int:
        return len(self._provider_classes)


registry = ProviderRegistry()
