from copy import deepcopy
import itertools
import pickle
from typing import Dict

import numpy as np
import pandas as pd
import pytest
from sklearn.preprocessing import StandardScaler
from sklearn.utils.validation import NotFittedError, check_is_fitted
import torch

from pytorch_forecasting.data import (
    EncoderNormalizer,
    GroupNormalizer,
    NaNLabelEncoder,
    TimeSeriesDataSet,
    TimeSynchronizedBatchSampler,
)
from pytorch_forecasting.data.encoders import MultiNormalizer, TorchNormalizer
from pytorch_forecasting.data.examples import get_stallion_data
from pytorch_forecasting.data.timeseries import _find_end_indices
from pytorch_forecasting.utils import to_list

torch.manual_seed(23)


@pytest.mark.parametrize(
    "data,allow_nan",
    itertools.product(
        [
            (np.array([2, 3, 4]), np.array([1, 2, 3, 5, np.nan])),
            (np.array(["a", "b", "c"]), np.array(["q", "a", "nan"])),
        ],
        [True, False],
    ),
)
def test_NaNLabelEncoder(data, allow_nan):
    fit_data, transform_data = data
    encoder = NaNLabelEncoder(warn=False, add_nan=allow_nan)
    encoder.fit(fit_data)
    assert np.array_equal(
        encoder.inverse_transform(encoder.transform(fit_data)), fit_data
    ), "Inverse transform should reverse transform"
    if not allow_nan:
        with pytest.raises(KeyError):
            encoder.transform(transform_data)
    else:
        assert encoder.transform(transform_data)[0] == 0, "First value should be translated to 0 if nan"
        assert encoder.transform(transform_data)[-1] == 0, "Last value should be translated to 0 if nan"
        assert encoder.transform(fit_data)[0] > 0, "First value should not be 0 if not nan"


def test_NaNLabelEncoder_add():
    encoder = NaNLabelEncoder(add_nan=False)
    encoder.fit(np.array(["a", "b", "c"]))
    encoder2 = deepcopy(encoder)
    encoder2.fit(np.array(["d"]))
    assert encoder2.transform(np.array(["a"]))[0] == 0, "a must be encoded as 0"
    assert encoder2.transform(np.array(["d"]))[0] == 3, "d must be encoded as 3"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(method="robust"),
        dict(method="robust", data=np.random.randn(100)),
        dict(data=np.random.randn(100)),
        dict(transformation="log"),
        dict(transformation="softplus"),
        dict(transformation="log1p"),
        dict(transformation="relu"),
        dict(method="identity"),
        dict(method="identity", data=np.random.randn(100)),
        dict(center=False),
        dict(max_length=5),
        dict(data=pd.Series(np.random.randn(100))),
        dict(max_length=[1, 2]),
    ],
)
def test_EncoderNormalizer(kwargs):
    kwargs.setdefault("method", "standard")
    kwargs.setdefault("center", True)
    kwargs.setdefault("data", torch.rand(100))
    data = kwargs.pop("data")

    normalizer = EncoderNormalizer(**kwargs)
    if kwargs.get("transformation") in ["relu", "softplus"]:
        data = data - 0.5

    if kwargs.get("transformation") in ["relu", "softplus", "log1p"]:
        assert (
            normalizer.inverse_transform(torch.as_tensor(normalizer.fit_transform(data))) >= 0
        ).all(), "Inverse transform should yield only positive values"
    else:
        assert torch.isclose(
            normalizer.inverse_transform(torch.as_tensor(normalizer.fit_transform(data))),
            torch.as_tensor(data),
            atol=1e-5,
        ).all(), "Inverse transform should reverse transform"


def test_EncoderNormalizer_with_limited_history():
    data = torch.rand(100)
    normalizer = EncoderNormalizer(max_length=[1, 2]).fit(data)
    assert normalizer.center_ == data[-1]


@pytest.mark.parametrize(
    "kwargs,groups",
    itertools.product(
        [
            dict(method="robust"),
            dict(transformation="log"),
            dict(transformation="relu"),
            dict(center=False),
            dict(transformation="log1p"),
            dict(transformation="softplus"),
            dict(scale_by_group=True),
        ],
        [[], ["a"]],
    ),
)
def test_GroupNormalizer(kwargs, groups):
    data = pd.DataFrame(dict(a=[1, 1, 2, 2, 3], b=[1.1, 1.1, 1.0, 5.0, 1.1]))
    defaults = dict(method="standard", transformation=None, center=True, scale_by_group=False)
    defaults.update(kwargs)
    kwargs = defaults
    kwargs["groups"] = groups
    kwargs["scale_by_group"] = kwargs["scale_by_group"] and len(kwargs["groups"]) > 0

    if kwargs.get("transformation") in ["relu", "softplus"]:
        data.b = data.b - 2.0
    normalizer = GroupNormalizer(**kwargs)
    encoded = normalizer.fit_transform(data["b"], data)

    test_data = dict(
        prediction=torch.tensor([encoded[0]]),
        target_scale=torch.tensor(normalizer.get_parameters([1])).unsqueeze(0),
    )

    if kwargs.get("transformation") in ["relu", "softplus", "log1p"]:
        assert (normalizer(test_data) >= 0).all(), "Inverse transform should yield only positive values"
    else:
        assert torch.isclose(
            normalizer(test_data), torch.tensor(data.b.iloc[0]), atol=1e-5
        ).all(), "Inverse transform should reverse transform"


def test_MultiNormalizer_fitted():
    data = pd.DataFrame(dict(a=[1, 1, 2, 2, 3], b=[1.1, 1.1, 1.0, 5.0, 1.1], c=[1.1, 1.1, 1.0, 5.0, 1.1]))

    normalizer = MultiNormalizer([GroupNormalizer(groups=["a"]), TorchNormalizer()])

    with pytest.raises(NotFittedError):
        check_is_fitted(normalizer)

    normalizer.fit(data, data)

    try:
        check_is_fitted(normalizer.normalizers[0])
        check_is_fitted(normalizer.normalizers[1])
        check_is_fitted(normalizer)
    except NotFittedError:
        pytest.fail(f"{NotFittedError}")


def check_dataloader_output(dataset: TimeSeriesDataSet, out: Dict[str, torch.Tensor]):
    x, y = out

    assert isinstance(y, tuple), "y output should be tuple of wegith and target"

    # check for nans and finite
    for k, v in x.items():
        for vi in to_list(v):
            assert torch.isfinite(vi).all(), f"Values for {k} should be finite"
            assert not torch.isnan(vi).any(), f"Values for {k} should not be nan"

    # check weight
    assert y[1] is None or isinstance(y[1], torch.Tensor), "weights should be none or tensor"
    if isinstance(y[1], torch.Tensor):
        assert torch.isfinite(y[1]).all(), "Values for weight should be finite"
        assert not torch.isnan(y[1]).any(), "Values for weight should not be nan"

    # check target
    for targeti in to_list(y[0]):
        assert torch.isfinite(targeti).all(), "Values for target should be finite"
        assert not torch.isnan(targeti).any(), "Values for target should not be nan"

    # check shape
    assert x["encoder_cont"].size(2) == len(dataset.reals)
    assert x["encoder_cat"].size(2) == len(dataset.flat_categoricals)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(min_encoder_length=0, max_prediction_length=2),
        dict(static_categoricals=["agency", "sku"]),
        dict(static_reals=["avg_population_2017", "avg_yearly_household_income_2017"]),
        dict(time_varying_known_categoricals=["month"]),
        dict(
            time_varying_known_categoricals=["special_days", "month"],
            variable_groups=dict(
                special_days=[
                    "easter_day",
                    "good_friday",
                    "new_year",
                    "christmas",
                    "labor_day",
                    "independence_day",
                    "revolution_day_memorial",
                    "regional_games",
                    "fifa_u_17_world_cup",
                    "football_gold_cup",
                    "beer_capital",
                    "music_fest",
                ]
            ),
        ),
        dict(time_varying_known_reals=["time_idx", "price_regular", "discount_in_percent"]),
        dict(time_varying_unknown_reals=["volume", "log_volume", "industry_volume", "soda_volume", "avg_max_temp"]),
        dict(
            target_normalizer=GroupNormalizer(
                groups=["agency", "sku"],
                transformation="log1p",
                scale_by_group=True,
            )
        ),
        dict(target_normalizer=EncoderNormalizer(), min_encoder_length=2),
        dict(randomize_length=True, min_encoder_length=2, min_prediction_length=1),
        dict(predict_mode=True),
        dict(add_target_scales=True),
        dict(add_encoder_length=True),
        dict(add_encoder_length=True),
        dict(add_relative_time_idx=True),
        dict(weight="volume"),
        dict(
            scalers=dict(time_idx=GroupNormalizer(), price_regular=StandardScaler()),
            categorical_encoders=dict(month=NaNLabelEncoder()),
            time_varying_known_categoricals=["month"],
            time_varying_known_reals=["time_idx", "price_regular"],
        ),
        dict(categorical_encoders={"month": NaNLabelEncoder(add_nan=True)}, time_varying_known_categoricals=["month"]),
        dict(constant_fill_strategy=dict(volume=0.0), allow_missing_timesteps=True),
        dict(target_normalizer=None),
    ],
)
def test_TimeSeriesDataSet(test_data, kwargs):

    defaults = dict(
        time_idx="time_idx",
        target="volume",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
    )
    defaults.update(kwargs)
    kwargs = defaults

    if kwargs.get("allow_missing_timesteps", False):
        np.random.seed(2)
        test_data = test_data.sample(frac=0.5)
        defaults["min_encoder_length"] = 0
        defaults["min_prediction_length"] = 1

    # create dataset and sample from it
    dataset = TimeSeriesDataSet(test_data, **kwargs)
    check_dataloader_output(dataset, next(iter(dataset.to_dataloader(num_workers=0))))


def test_from_dataset(test_dataset, test_data):
    dataset = TimeSeriesDataSet.from_dataset(test_dataset, test_data)
    check_dataloader_output(dataset, next(iter(dataset.to_dataloader(num_workers=0))))


def test_from_dataset_equivalence(test_data):
    training = TimeSeriesDataSet(
        test_data[lambda x: x.time_idx < x.time_idx.max() - 1],
        time_idx="time_idx",
        target="volume",
        time_varying_known_reals=["price_regular", "time_idx"],
        group_ids=["agency", "sku"],
        static_categoricals=["agency"],
        max_encoder_length=3,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=0,
        randomize_length=None,
        add_encoder_length=True,
        add_relative_time_idx=True,
        add_target_scales=True,
    )
    validation1 = TimeSeriesDataSet.from_dataset(training, test_data, predict=True)
    validation2 = TimeSeriesDataSet.from_dataset(
        training,
        test_data[lambda x: x.time_idx > x.time_idx.min() + 2],
        predict=True,
    )
    # ensure validation1 and validation2 datasets are exactly the same despite different data inputs
    for v1, v2 in zip(iter(validation1.to_dataloader(train=False)), iter(validation2.to_dataloader(train=False))):
        for k in v1[0].keys():
            if isinstance(v1[0][k], (tuple, list)):
                assert len(v1[0][k]) == len(v2[0][k])
                for idx in range(len(v1[0][k])):
                    assert torch.isclose(v1[0][k][idx], v2[0][k][idx]).all()
            else:
                assert torch.isclose(v1[0][k], v2[0][k]).all()
        assert torch.isclose(v1[1][0], v2[1][0]).all()


def test_dataset_index(test_dataset):
    index = []
    for x, _ in iter(test_dataset.to_dataloader()):
        index.append(test_dataset.x_to_index(x))
    index = pd.concat(index, axis=0, ignore_index=True)
    assert len(index) <= len(test_dataset), "Index can only be subset of dataset"


@pytest.mark.parametrize("min_prediction_idx", [0, 1, 3, 7])
def test_min_prediction_idx(test_dataset, test_data, min_prediction_idx):
    dataset = TimeSeriesDataSet.from_dataset(
        test_dataset, test_data, min_prediction_idx=min_prediction_idx, min_encoder_length=1, max_prediction_length=10
    )

    for x, _ in iter(dataset.to_dataloader(num_workers=0, batch_size=1000)):
        assert x["decoder_time_idx"].min() >= min_prediction_idx


@pytest.mark.parametrize(
    "value,variable,target",
    [
        (1.0, "price_regular", "encoder"),
        (1.0, "price_regular", "all"),
        (1.0, "price_regular", "decoder"),
        ("Agency_01", "agency", "all"),
        ("Agency_01", "agency", "decoder"),
    ],
)
def test_overwrite_values(test_dataset, value, variable, target):
    dataset = deepcopy(test_dataset)

    # create variables to check against
    control_outputs = next(iter(dataset.to_dataloader(num_workers=0, train=False)))
    dataset.set_overwrite_values(value, variable=variable, target=target)

    # test change
    outputs = next(iter(dataset.to_dataloader(num_workers=0, train=False)))
    check_dataloader_output(dataset, outputs)

    if variable in dataset.reals:
        output_name_suffix = "cont"
    else:
        output_name_suffix = "cat"

    if target == "all":
        output_names = [f"encoder_{output_name_suffix}", f"decoder_{output_name_suffix}"]
    else:
        output_names = [f"{target}_{output_name_suffix}"]

    for name in outputs[0].keys():
        changed = torch.isclose(outputs[0][name], control_outputs[0][name]).all()
        if name in output_names or (
            "cat" in name and variable == "agency"
        ):  # exception for static categorical which should always change
            assert not changed, f"Output {name} should change"
        else:
            assert changed, f"Output {name} should not change"

    # test resetting
    dataset.reset_overwrite_values()
    outputs = next(iter(dataset.to_dataloader(num_workers=0, train=False)))
    for name in outputs[0].keys():
        changed = torch.isclose(outputs[0][name], control_outputs[0][name]).all()
        assert changed, f"Output {name} should be reset"
    assert torch.isclose(outputs[1][0], control_outputs[1][0]).all(), "Target should be reset"


@pytest.mark.parametrize(
    "drop_last,shuffle,as_string,batch_size",
    [
        (True, True, True, 64),
        (False, False, False, 64),
        (True, False, False, 1000),
    ],
)
def test_TimeSynchronizedBatchSampler(test_dataset, shuffle, drop_last, as_string, batch_size):
    if as_string:
        dataloader = test_dataset.to_dataloader(
            batch_sampler="synchronized", shuffle=shuffle, drop_last=drop_last, batch_size=batch_size
        )
    else:
        sampler = TimeSynchronizedBatchSampler(
            data_source=test_dataset, shuffle=shuffle, drop_last=drop_last, batch_size=batch_size
        )
        dataloader = test_dataset.to_dataloader(batch_sampler=sampler)

    time_idx_pos = test_dataset.reals.index("time_idx")
    for x, _ in iter(dataloader):  # check all samples
        time_idx_of_first_prediction = x["decoder_cont"][:, 0, time_idx_pos]
        assert torch.isclose(
            time_idx_of_first_prediction, time_idx_of_first_prediction[0]
        ).all(), "Time index should be the same for the first prediction"


def test_find_end_indices():
    diffs = np.array([1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1])
    max_lengths = np.array([4, 4, 4, 4, 4, 4, 4, 4, 3, 2, 1, 4, 4, 4, 4, 4, 4, 4, 4, 3, 2, 1])

    ends, missings = _find_end_indices(diffs, max_lengths, min_length=3)
    ends_test = np.array([3, 4, 4, 5, 6, 8, 9, 10, 10, 10, 10, 14, 15, 15, 16, 17, 19, 20, 21, 21, 21, 21])
    missings_test = np.array([[0, 2], [5, 7], [11, 13], [16, 18]])
    np.testing.assert_array_equal(ends, ends_test)
    np.testing.assert_array_equal(missings, missings_test)


def test_raise_short_encoder_length(test_data):
    with pytest.warns(UserWarning):
        test_data = test_data[lambda x: ~((x.agency == "Agency_22") & (x.sku == "SKU_01") & (x.time_idx > 3))]
        TimeSeriesDataSet(
            test_data,
            time_idx="time_idx",
            target="volume",
            group_ids=["agency", "sku"],
            max_encoder_length=5,
            max_prediction_length=2,
            min_prediction_length=1,
            min_encoder_length=5,
        )


def test_categorical_target(test_data):
    dataset = TimeSeriesDataSet(
        test_data,
        time_idx="time_idx",
        target="agency",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
    )

    _, y = next(iter(dataset.to_dataloader()))
    assert y[0].dtype is torch.long, "target must be of type long"


def test_pickle(test_dataset):
    pickle.dumps(test_dataset)
    pickle.dumps(test_dataset.to_dataloader())


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        dict(
            target_normalizer=GroupNormalizer(groups=["agency", "sku"], transformation="log1p", scale_by_group=True),
        ),
    ],
)
def test_new_group_ids(test_data, kwargs):
    """Test for new group ids in dataset"""
    train_agency = test_data["agency"].iloc[0]
    train_dataset = TimeSeriesDataSet(
        test_data[lambda x: x.agency == train_agency],
        time_idx="time_idx",
        target="volume",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
        categorical_encoders=dict(agency=NaNLabelEncoder(add_nan=True), sku=NaNLabelEncoder(add_nan=True)),
        **kwargs,
    )

    # test sampling from training dataset
    next(iter(train_dataset.to_dataloader()))

    # create test dataset with group ids that have not been observed before
    test_dataset = TimeSeriesDataSet.from_dataset(train_dataset, test_data)

    # check that we can iterate through dataset without error
    for _ in iter(test_dataset.to_dataloader()):
        pass


def test_timeseries_columns_naming(test_data):
    with pytest.raises(ValueError):
        TimeSeriesDataSet(
            test_data.rename(columns=dict(agency="agency.2")),
            time_idx="time_idx",
            target="volume",
            group_ids=["agency.2", "sku"],
            max_encoder_length=5,
            max_prediction_length=2,
            min_prediction_length=1,
            min_encoder_length=1,
        )


def test_encoder_normalizer_for_covariates(test_data):
    dataset = TimeSeriesDataSet(
        test_data,
        time_idx="time_idx",
        target="volume",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
        time_varying_known_reals=["price_regular"],
        scalers={"price_regular": EncoderNormalizer()},
    )
    next(iter(dataset.to_dataloader()))


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        dict(
            target_normalizer=MultiNormalizer(normalizers=[TorchNormalizer(), EncoderNormalizer()]),
        ),
        dict(add_target_scales=True),
        dict(weight="volume"),
    ],
)
def test_multitarget(test_data, kwargs):
    dataset = TimeSeriesDataSet(
        test_data.assign(volume1=lambda x: x.volume),
        time_idx="time_idx",
        target=["volume", "volume1"],
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
        time_varying_known_reals=["price_regular"],
        scalers={"price_regular": EncoderNormalizer()},
        **kwargs,
    )
    next(iter(dataset.to_dataloader()))


def test_check_nas(test_data):
    data = test_data.copy()
    data.loc[0, "volume"] = np.nan
    with pytest.raises(ValueError, match=r"1 \(.*infinite"):
        TimeSeriesDataSet(
            data,
            time_idx="time_idx",
            target=["volume"],
            group_ids=["agency", "sku"],
            max_encoder_length=5,
            max_prediction_length=2,
            min_prediction_length=1,
            min_encoder_length=1,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(target="volume"),
        dict(target="agency", scalers={"volume": EncoderNormalizer()}),
        dict(target="volume", target_normalizer=EncoderNormalizer()),
        dict(target=["volume", "agency"]),
    ],
)
def test_lagged_variables(test_data, kwargs):
    dataset = TimeSeriesDataSet(
        test_data.copy(),
        time_idx="time_idx",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=3,  # one more than max lag for validation
        time_varying_unknown_reals=["volume"],
        time_varying_unknown_categoricals=["agency"],
        lags={"volume": [1, 2], "agency": [1, 2]},
        add_encoder_length=False,
        **kwargs,
    )

    x_all, _ = next(iter(dataset.to_dataloader()))

    for name in ["volume", "agency"]:
        if name in dataset.reals:
            vars = dataset.reals
            x = x_all["encoder_cont"]
        else:
            vars = dataset.flat_categoricals
            x = x_all["encoder_cat"]
        target_idx = vars.index(name)
        for lag in [1, 2]:
            lag_idx = vars.index(f"{name}_lagged_by_{lag}")
            target = x[..., target_idx][:, 0]
            lagged_target = torch.roll(x[..., lag_idx], -lag, dims=1)[:, 0]
            assert torch.isclose(target, lagged_target).all(), "lagged target must be the same as non-lagged target"


@pytest.mark.parametrize(
    "agency,first_prediction_idx,should_raise",
    [("Agency_01", 0, False), ("xxxxx", 0, True), ("Agency_01", 100, True), ("Agency_01", 4, False)],
)
def test_filter_data(test_dataset, agency, first_prediction_idx, should_raise):
    func = lambda x: (x.agency == agency) & (x.time_idx_first_prediction >= first_prediction_idx)
    if should_raise:
        with pytest.raises(ValueError):
            test_dataset.filter(func)
    else:
        filtered_dataset = test_dataset.filter(func)
        assert len(test_dataset.index) > len(
            filtered_dataset.index
        ), "filtered dataset should have less entries than original dataset"
        for x, _ in iter(filtered_dataset.to_dataloader()):
            index = test_dataset.x_to_index(x)
            assert (index["agency"] == agency).all(), "Agency filter has failed"
            assert index["time_idx"].min() == first_prediction_idx, "First prediction filter has failed"


def test_TorchNormalizer_dtype_consistency():
    """Ensures that even for float64 `target_scale`, the transformation will not change the prediction dtype."""
    parameters = torch.tensor([[[366.4587]]])
    target_scale = torch.tensor([[427875.7500, 80367.4766]], dtype=torch.float64)
    assert TorchNormalizer()(dict(prediction=parameters, target_scale=target_scale)).dtype == torch.float32
    assert TorchNormalizer().transform(parameters, target_scale=target_scale).dtype == torch.float32
