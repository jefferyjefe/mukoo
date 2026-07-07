"""Mukoo coverage model (placeholder).

This package will turn the point measurements collected by the ingestion API
into continuous coverage surfaces — e.g. ordinary kriging of RSRP, blended with
an RF propagation prior — plus dead-zone polygons. It is a separate installable
package so the modelling pipeline can run and scale independently of the API.

Nothing is implemented yet; the scaffolding exists so the package is importable
and its dependencies can be added in isolation.
"""

__version__ = "0.1.0"
