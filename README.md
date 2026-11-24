# 绿色制造申报证据采集与复审续期

合成绿色制造申报范围、能源证据及评审决定样本。金额为人民币元，生效日期为本地日期。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `sites.csv` contains 企业提交的厂区边界和版本。 Columns are `site`, `company`, `area_m2`, `valid_from`, `revision`.
- `evidence.csv` contains 能源、用水和第三方证明的有效期间及范围。 Columns are `evidence`, `site`, `kind`, `value`, `unit`, `from`, `to`, `revision`.
- `decisions.csv` contains 已签发的评审结论及覆盖版本。 Columns are `decision`, `site`, `period`, `result`, `site_revision`, `evidence_refs`, `state`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
