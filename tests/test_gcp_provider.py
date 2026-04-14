"""Unit tests for GCP provider functionality.

These tests verify the GCP provider can be instantiated and its
methods work correctly. They use mocks to avoid requiring actual
GCP credentials.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from ephemeral_forge.config import GCPConfig
from ephemeral_forge.providers.gcp import GCPProvider
from ephemeral_forge.provider import FleetConfig


def test_gcp_provider_instantiation():
    """Test that GCPProvider can be instantiated with a valid config."""
    config = GCPConfig(
        project_id="test-project",
        ssh_user="ubuntu",
    )
    provider = GCPProvider(config)
    assert provider.config == config
    assert provider._project == "test-project"


def test_gcp_provider_requires_project_id():
    """Test that GCPProvider raises an error without project_id."""
    config = GCPConfig(
        project_id="",
        ssh_user="ubuntu",
    )
    with pytest.raises(ValueError, match="project_id is required"):
        GCPProvider(config)


def test_default_instance_types():
    """Test that default instance types are returned."""
    config = GCPConfig(
        project_id="test-project",
        ssh_user="ubuntu",
    )
    provider = GCPProvider(config)
    types = provider.default_instance_types
    assert len(types) > 0
    assert "e2-standard-2" in types
    assert "n2-standard-2" in types


def test_default_gpu_instance_types():
    """Test that default GPU instance types are returned."""
    config = GCPConfig(
        project_id="test-project",
        ssh_user="ubuntu",
    )
    provider = GCPProvider(config)
    types = provider.default_gpu_instance_types
    assert len(types) > 0
    assert "g2-standard-4" in types


@patch("ephemeral_forge.providers.gcp.compute_v1.ZonesClient")
def test_probe_spot_prices_returns_list(MockZonesClient):
    """Test that probe_spot_prices returns a list of SpotPrice objects."""
    # Setup mock
    mock_client = MagicMock()
    mock_zone = MagicMock()
    mock_zone.name = "us-central1-a"
    mock_zone.status = "UP"
    mock_client.list.return_value = [mock_zone]
    MockZonesClient.return_value = mock_client

    # Mock the async probing
    with patch.object(GCPProvider, "_probe_all_zones") as mock_probe:
        from ephemeral_forge.provider import SpotPrice

        mock_probe.return_value = [
            SpotPrice(
                region="us-central1",
                zone="us-central1-a",
                instance_type="e2-standard-2",
                price_per_hour=0.013,
            )
        ]

        config = GCPConfig(
            project_id="test-project",
            ssh_user="ubuntu",
        )
        provider = GCPProvider(config)
        result = provider.probe_spot_prices(["e2-standard-2"])

    assert len(result) == 1
    assert result[0].instance_type == "e2-standard-2"
    assert result[0].region == "us-central1"


@patch("ephemeral_forge.providers.gcp.compute_v1.ImagesClient")
def test_resolve_image_returns_url(MockImagesClient):
    """Test that resolve_image returns a valid image URL."""
    mock_client = MagicMock()
    mock_image = MagicMock()
    mock_image.self_link = "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
    mock_client.get_from_family.return_value = mock_image
    MockImagesClient.return_value = mock_client

    config = GCPConfig(
        project_id="test-project",
        ssh_user="ubuntu",
    )
    provider = GCPProvider(config)
    result = provider.resolve_image("us-central1")

    assert "ubuntu-os-cloud" in result
    mock_client.get_from_family.assert_called_once()


@patch("ephemeral_forge.providers.gcp.compute_v1.ImagesClient")
def test_resolve_image_gpu(MockImagesClient):
    """Test that resolve_image returns a GPU image when gpu=True."""
    mock_client = MagicMock()
    mock_image = MagicMock()
    mock_image.self_link = "https://www.googleapis.com/compute/v1/projects/deeplearning-platform-release/global/images/family/pytorch-latest-gpu-ubuntu-2204-py310"
    mock_client.get_from_family.return_value = mock_image
    MockImagesClient.return_value = mock_client

    config = GCPConfig(
        project_id="test-project",
        ssh_user="ubuntu",
    )
    provider = GCPProvider(config)
    result = provider.resolve_image("us-central1", gpu=True)

    assert "deeplearning-platform-release" in result


def test_provider_get_factory():
    """Test that the provider factory can instantiate GCPProvider."""
    from ephemeral_forge.config import Config, GCPConfig
    from ephemeral_forge.providers import get_provider

    config = Config(
        gcp=GCPConfig(
            project_id="test-project",
            ssh_user="ubuntu",
        )
    )
    provider = get_provider("gcp", config)
    assert isinstance(provider, GCPProvider)


def test_fleet_config_with_gcp_types():
    """Test that FleetConfig accepts GCP instance types."""
    config = FleetConfig(
        count=2,
        instance_types=["e2-standard-2", "e2-standard-4"],
        ssh_user="ubuntu",
        disk_gb=20,
    )
    assert config.count == 2
    assert "e2-standard-2" in config.instance_types
    assert config.disk_gb >= 10  # GCP requires >= 10 GB for Ubuntu


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
