"""Target-blind maps and three-dimensional, bounded search policies."""
import numpy as np

METHODS = ('online_quadratic', 'stored_quadratic', 'feature_gp', 'coordinate')


def lhs(seed, count):
    rng = np.random.default_rng(seed)
    return np.column_stack([(rng.permutation(count) + rng.random(count)) / count for _ in range(3)])


def basis(x):
    x = np.atleast_2d(x)
    return np.column_stack([np.ones(len(x)), x, x*x, x[:, 0]*x[:, 1],
                            x[:, 0]*x[:, 2], x[:, 1]*x[:, 2]])


def quadratic(x, y, pool):
    a = basis(x)
    return basis(pool) @ np.linalg.solve(a.T @ a + 1e-4*np.eye(10), a.T @ y)


def rms(y, target):
    return np.sqrt(np.mean((np.asarray(y) - target)**2, axis=-1))


class FeatureGP:
    """Independent feature GPs; ARD lengths, nugget and amplitude fitted by ML."""
    def __init__(self, x, y):
        self.x = np.asarray(x)
        self.mean = float(np.mean(y))
        self.y = np.asarray(y) - self.mean
        self.d2 = (self.x[:, None, :] - self.x[None, :, :])**2
        theta = np.array([.4, .4, .4, 1e-5])
        self.initial_nll = self.fit_at(theta)[0]
        best = self.initial_nll
        # Two deterministic coordinate passes, independent of the target value.
        for _ in range(2):
            for axis in range(4):
                values = (.1, .2, .4, .8, 1.6) if axis < 3 else (1e-7, 1e-5, 1e-3, .03)
                for value in values:
                    proposal = theta.copy(); proposal[axis] = value
                    score = self.fit_at(proposal)[0]
                    if score < best:
                        theta, best = proposal, score
        self.theta = theta
        self.nll, self.l, self.alpha, self.amplitude = self.fit_at(theta)

    def fit_at(self, theta):
        k = np.exp(-.5 * np.sum(self.d2 / theta[:3]**2, axis=2))
        l = np.linalg.cholesky(k + (theta[3] + 1e-10)*np.eye(len(k)))
        alpha = np.linalg.solve(l.T, np.linalg.solve(l, self.y))
        amplitude = max(float(self.y @ alpha / len(k)), 1e-12)
        nll = .5*len(k)*np.log(amplitude) + np.log(np.diag(l)).sum() + .5*len(k)
        return float(nll), l, alpha, amplitude

    def predict(self, pool):
        d2 = (self.x[:, None, :] - pool[None, :, :])**2
        k = np.exp(-.5 * np.sum(d2 / self.theta[:3]**2, axis=2))
        mean = self.mean + k.T @ self.alpha
        v = np.linalg.solve(self.l, k)
        variance = self.amplitude * np.maximum(1 - np.sum(v*v, axis=0), 0)
        return mean, variance


class Search:
    def __init__(self, method, seed, target, ranges, stored=None):
        if method not in METHODS:
            raise ValueError(method)
        if (method == 'stored_quadratic') != (stored is not None):
            raise ValueError('Only the warm method receives the target-blind stored map')
        self.method, self.seed = method, seed
        self.ranges = np.asarray(ranges)
        self.target = np.asarray(target) / self.ranges
        self.rng = np.random.default_rng(seed + 100000)
        self.initial = lhs(seed, 10)
        self.x, self.y = [], []
        self.stored = stored
        self.last_fit = []

    def tell(self, x, features):
        self.x.append(list(x)); self.y.append((np.asarray(features) / self.ranges).tolist())

    def ask(self):
        n = len(self.x)
        initial_count = {'online_quadratic': 10, 'stored_quadratic': 0, 'feature_gp': 5, 'coordinate': 1}[self.method]
        if n < initial_count:
            return self.initial[n].copy()
        x, y = np.array(self.x), np.array(self.y)
        if self.method == 'coordinate':
            best = x[np.argmin(rms(y, self.target))]
            step = n - 1
            axis = (step + self.seed) % 3
            sign = 1 if ((step // 3 + self.seed) % 2) else -1
            proposal = best.copy()
            proposal[axis] = np.clip(proposal[axis] + sign*.25/(2**(step//6)), 0, 1)
            if np.min(np.linalg.norm(x-proposal, axis=1)) < .001:
                proposal[axis] = np.clip(best[axis] - sign*.25/(2**(step//6)), 0, 1)
            if np.min(np.linalg.norm(x-proposal, axis=1)) < .001:
                proposal = self.rng.uniform(size=3)
            return proposal
        pool = self.rng.uniform(size=(512, 3))
        if n:
            best = x[np.argmin(rms(y, self.target))]
            pool = np.vstack([pool, np.clip(best + self.rng.normal(0, .12, (128, 3)), 0, 1)])
            pool = pool[np.min(np.linalg.norm(pool[:, None, :]-x[None, :, :], axis=2), axis=1) >= .001]
        if self.method in ('online_quadratic', 'stored_quadratic'):
            if self.stored is not None:
                sx, sy = np.asarray(self.stored['x']), np.asarray(self.stored['features'])/self.ranges
                x = np.vstack([sx, x]) if n else sx
                y = np.vstack([sy, y]) if n else sy
            return pool[np.argmin(rms(quadratic(x, y, pool), self.target))]
        means, variances = [], []
        self.last_fit = []
        for j in range(y.shape[1]):
            gp = FeatureGP(x, y[:, j])
            mean, variance = gp.predict(pool)
            means.append(mean); variances.append(variance)
            self.last_fit.append({'theta': gp.theta.tolist(), 'amplitude': gp.amplitude,
                                  'nll': gp.nll, 'initial_nll': gp.initial_nll})
        mean, sd = np.array(means).T, np.sqrt(np.array(variances).T)
        draws = mean[None, :, :] + sd[None, :, :]*self.rng.normal(size=(32, len(pool), len(means)))
        improvement = np.maximum(np.min(rms(y, self.target)) - rms(draws, self.target), 0).mean(axis=0)
        if np.max(improvement) <= 0:
            return pool[np.argmin(rms(mean, self.target))]
        return pool[np.argmax(improvement)]
