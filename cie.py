"""Color math: camera sRGB to CIE 1976 u'v', CCT and Duv, the Planckian and daylight loci."""
import numpy as np

# sRGB-encoded 8-bit value -> linear light
_v = np.arange(256) / 255.0
SRGB_TO_LINEAR = np.where(_v <= 0.04045, _v / 12.92, ((_v + 0.055) / 1.055) ** 2.4).astype(np.float32)

M_SRGB_TO_XYZ = np.array([[0.4124, 0.3576, 0.1805],
                          [0.2126, 0.7152, 0.0722],
                          [0.0193, 0.1192, 0.9505]])

# CIE 1931 2-degree color matching functions at 10 nm, 380..780 nm
_CMF = np.array([
    [0.001368, 0.000039, 0.006450], [0.004243, 0.000120, 0.020050], [0.014310, 0.000396, 0.067850],
    [0.043510, 0.001210, 0.207400], [0.134380, 0.004000, 0.645600], [0.283900, 0.011600, 1.385600],
    [0.348280, 0.023000, 1.747060], [0.336200, 0.038000, 1.772110], [0.290800, 0.060000, 1.669200],
    [0.195360, 0.090980, 1.287640], [0.095640, 0.139020, 0.812950], [0.032010, 0.208020, 0.465180],
    [0.004900, 0.323000, 0.272000], [0.009300, 0.503000, 0.158200], [0.063270, 0.710000, 0.078250],
    [0.165500, 0.862000, 0.042160], [0.290400, 0.954000, 0.020300], [0.433450, 0.994950, 0.008750],
    [0.594500, 0.995000, 0.003900], [0.762100, 0.952000, 0.002100], [0.916300, 0.870000, 0.001650],
    [1.026300, 0.757000, 0.001100], [1.062200, 0.631000, 0.000800], [1.002600, 0.503000, 0.000340],
    [0.854450, 0.381000, 0.000190], [0.642400, 0.265000, 0.000050], [0.447900, 0.175000, 0.000020],
    [0.283500, 0.107000, 0.000000], [0.164900, 0.061000, 0.000000], [0.087400, 0.032000, 0.000000],
    [0.046770, 0.017000, 0.000000], [0.022700, 0.008210, 0.000000], [0.011359, 0.004102, 0.000000],
    [0.005790, 0.002091, 0.000000], [0.002899, 0.001047, 0.000000], [0.001440, 0.000520, 0.000000],
    [0.000690, 0.000249, 0.000000], [0.000332, 0.000120, 0.000000], [0.000166, 0.000060, 0.000000],
    [0.000083, 0.000030, 0.000000], [0.000042, 0.000015, 0.000000]])
_WL = np.arange(380, 781, 10) * 1e-9


def xyz_to_uv(X, Y, Z):
    d = X + 15 * Y + 3 * Z
    return 4 * X / d, 9 * Y / d


def linear_rgb_to_uv(rgb):
    """Linear sRGB (camera space) -> CIE 1976 u', v'."""
    return xyz_to_uv(*(M_SRGB_TO_XYZ @ np.asarray(rgb, dtype=float)))


def planck_uv(T):
    """u', v' of a blackbody at T kelvin."""
    c1, c2 = 3.741771e-16, 1.4388e-2
    spd = c1 / _WL ** 5 / (np.exp(c2 / (_WL * T)) - 1)
    X, Y, Z = spd @ _CMF
    return xyz_to_uv(X, Y, Z)


def daylight_uv(T):
    """u', v' of the CIE daylight locus (the D illuminants) at nominal temperature T."""
    t = T * 1.4388 / 1.4380
    if t <= 7000:
        x = -4.6070e9 / t ** 3 + 2.9678e6 / t ** 2 + 0.09911e3 / t + 0.244063
    else:
        x = -2.0064e9 / t ** 3 + 1.9018e6 / t ** 2 + 0.24748e3 / t + 0.237040
    y = -3.000 * x * x + 2.870 * x - 0.275
    d = -2 * x + 12 * y + 3
    return 4 * x / d, 9 * y / d


_TS = np.concatenate([np.arange(2000, 10000, 10), np.arange(10000, 20001, 100)])
_LOC = np.array([planck_uv(t) for t in _TS])


def cct_duv(u, v):
    """Nearest-point CCT in kelvin and Duv in CIE 1960 uv (positive = above the locus = greener)."""
    loc = np.column_stack([_LOC[:, 0], _LOC[:, 1] * 2 / 3])    # CIE 1960 v = 2/3 v'
    p = np.array([u, v * 2 / 3])
    d = np.hypot(*(loc - p).T)
    i = int(np.argmin(d))
    lu, lv = loc[i]
    j = min(i + 1, len(loc) - 1)
    tu, tv = loc[j] - loc[max(i - 1, 0)]
    cross = tu * (p[1] - lv) - tv * (p[0] - lu)
    return float(_TS[i]), float(-np.sign(cross) * d[i])
