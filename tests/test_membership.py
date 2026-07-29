import pytest

from custom_components.fuzzy_thermostat.fuzzy.membership import Trapezoidal, Triangular


class TestTriangular:
    def test_geometry(self):
        mf = Triangular(0, 5, 10)
        assert mf(-1) == 0.0
        assert mf(0) == 0.0
        assert mf(2.5) == pytest.approx(0.5)
        assert mf(5) == 1.0
        assert mf(7.5) == pytest.approx(0.5)
        assert mf(10) == 0.0
        assert mf(11) == 0.0

    def test_left_shoulder(self):
        """trimf([a, a, c]) semantics: full membership at the shared breakpoint."""
        mf = Triangular(50, 50, 70)
        assert mf(50) == 1.0
        assert mf(60) == pytest.approx(0.5)
        assert mf(70) == 0.0
        assert mf(49.999) == 0.0

    def test_right_shoulder(self):
        mf = Triangular(70, 90, 90)
        assert mf(70) == 0.0
        assert mf(80) == pytest.approx(0.5)
        assert mf(90) == 1.0
        assert mf(90.001) == 0.0

    def test_peak(self):
        assert Triangular(0, 5, 10).peak == 5

    def test_rejects_unsorted(self):
        with pytest.raises(ValueError):
            Triangular(5, 0, 10)


class TestTrapezoidal:
    def test_geometry(self):
        mf = Trapezoidal(0, 2, 8, 10)
        assert mf(-1) == 0.0
        assert mf(1) == pytest.approx(0.5)
        assert mf(2) == 1.0
        assert mf(5) == 1.0
        assert mf(8) == 1.0
        assert mf(9) == pytest.approx(0.5)
        assert mf(10.5) == 0.0

    def test_open_shoulders(self):
        mf = Trapezoidal(0, 0, 3, 6)  # flat-topped left shoulder
        assert mf(0) == 1.0
        assert mf(3) == 1.0
        assert mf(4.5) == pytest.approx(0.5)

    def test_peak_is_plateau_midpoint(self):
        assert Trapezoidal(0, 2, 8, 10).peak == 5

    def test_rejects_unsorted(self):
        with pytest.raises(ValueError):
            Trapezoidal(0, 8, 2, 10)
