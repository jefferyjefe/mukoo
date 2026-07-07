# mukoo-model

Coverage modelling for Mukoo — a separate installable package that reads the
measurements collected by the ingestion API and produces coverage estimates.

Planned scope:

- **Interpolation** — ordinary/regression kriging of RSRP (and friends) across
  the road network and surrounding area.
- **Propagation prior** — a physical path-loss / terrain model used as a prior
  or covariate for the kriging.
- **Dead-zone extraction** — polygons where `network_type = 'none'` clusters.

Not yet implemented. See [pyproject.toml](pyproject.toml) for the package
definition; modelling dependencies are added there as the package grows.

```bash
pip install -e model
```
