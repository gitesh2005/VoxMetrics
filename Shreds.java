import java.awt.geom.Area;
import java.awt.geom.Path2D;
import java.util.*;

public class Shreds {
    static int n;
    static int[][] xs, ys;
    static int[] cnt, tx, ty;
    static long[] area2;
    static Set<String> visited;
    static long curTotal;

    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
        n = sc.nextInt();
        xs = new int[n][];
        ys = new int[n][];
        cnt = new int[n];
        area2 = new long[n];
        for (int i = 0; i < n; i++) {
            cnt[i] = sc.nextInt();
            xs[i] = new int[cnt[i]];
            ys[i] = new int[cnt[i]];
            for (int j = 0; j < cnt[i]; j++) {
                xs[i][j] = sc.nextInt();
                ys[i][j] = sc.nextInt();
            }
            area2[i] = shoelace(xs[i], ys[i]);
        }
        tx = new int[n];
        ty = new int[n];
        int full = (1 << n) - 1;
        long[] sum = new long[full + 1];
        for (int m = 1; m <= full; m++) {
            int low = Integer.numberOfTrailingZeros(m);
            sum[m] = sum[m & (m - 1)] + area2[low];
        }
        Integer[] order = new Integer[full];
        for (int m = 1; m <= full; m++) order[m - 1] = m;
        Arrays.sort(order, (a, b) -> Long.compare(sum[b], sum[a]));
        long best = 0;
        for (int mask : order) {
            if (sum[mask] <= best) break;
            visited = new HashSet<>();
            Arrays.fill(tx, 0);
            Arrays.fill(ty, 0);
            curTotal = sum[mask];
            int first = Integer.numberOfTrailingZeros(mask);
            if (dfs(mask, 1 << first)) {
                best = sum[mask];
                break;
            }
        }
        System.out.println(best / 2);
    }

    static long shoelace(int[] x, int[] y) {
        long s = 0;
        int len = x.length;
        for (int i = 0; i < len; i++) {
            int j = (i + 1) % len;
            s += (long) x[i] * y[j] - (long) x[j] * y[i];
        }
        return Math.abs(s);
    }

    static long encode(int dx, int dy) {
        return (dx + 100000L) * 200000L + (dy + 100000L);
    }

    static boolean dfs(int mask, int placed) {
        if (placed == mask) return isRect(mask);
        if (hullArea2(placed) > 2 * curTotal) return false;

        StringBuilder sb = new StringBuilder();
        sb.append(placed).append('|');
        for (int i = 0; i < n; i++) {
            if (((placed >> i) & 1) == 1) {
                sb.append(i).append(',').append(tx[i]).append(',').append(ty[i]).append(';');
            }
        }
        if (!visited.add(sb.toString())) return false;

        for (int p = 0; p < n; p++) {
            if (((mask >> p) & 1) == 0 || ((placed >> p) & 1) == 1) continue;
            Set<Long> cands = new HashSet<>();
            for (int a = 0; a < cnt[p]; a++) {
                for (int q = 0; q < n; q++) {
                    if (((placed >> q) & 1) == 0) continue;
                    for (int b = 0; b < cnt[q]; b++) {
                        int dx = xs[q][b] + tx[q] - xs[p][a];
                        int dy = ys[q][b] + ty[q] - ys[p][a];
                        cands.add(encode(dx, dy));
                    }
                }
            }
            for (long c : cands) {
                int dx = (int) (c / 200000L - 100000L);
                int dy = (int) (c % 200000L - 100000L);
                tx[p] = dx;
                ty[p] = dy;
                boolean ok = true;
                for (int q = 0; q < n && ok; q++) {
                    if (((placed >> q) & 1) == 1 && overlaps(p, q)) ok = false;
                }
                if (ok && dfs(mask, placed | (1 << p))) return true;
            }
            tx[p] = 0;
            ty[p] = 0;
        }
        return false;
    }

    static long hullArea2(int placed) {
        List<long[]> pts = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            if (((placed >> i) & 1) == 0) continue;
            for (int j = 0; j < cnt[i]; j++) {
                pts.add(new long[]{xs[i][j] + tx[i], ys[i][j] + ty[i]});
            }
        }
        pts.sort((a, b) -> a[0] != b[0] ? Long.compare(a[0], b[0]) : Long.compare(a[1], b[1]));
        int m = pts.size();
        long[][] h = new long[2 * m + 2][];
        int t = 0;
        for (int i = 0; i < m; i++) {
            while (t >= 2 && crossL(h[t - 2], h[t - 1], pts.get(i)) <= 0) t--;
            h[t++] = pts.get(i);
        }
        for (int i = m - 2, lo = t + 1; i >= 0; i--) {
            while (t >= lo && crossL(h[t - 2], h[t - 1], pts.get(i)) <= 0) t--;
            h[t++] = pts.get(i);
        }
        int hs = t - 1;
        long area = 0;
        for (int i = 0; i < hs; i++) {
            int j = (i + 1) % hs;
            area += h[i][0] * h[j][1] - h[j][0] * h[i][1];
        }
        return Math.abs(area);
    }

    static long crossL(long[] o, long[] a, long[] b) {
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    }

    static Path2D.Double path(int p) {
        Path2D.Double path = new Path2D.Double();
        path.moveTo(xs[p][0] + tx[p], ys[p][0] + ty[p]);
        for (int k = 1; k < cnt[p]; k++) {
            path.lineTo(xs[p][k] + tx[p], ys[p][k] + ty[p]);
        }
        path.closePath();
        return path;
    }

    static int[] bounds(int p) {
        int minX = Integer.MAX_VALUE, minY = Integer.MAX_VALUE;
        int maxX = Integer.MIN_VALUE, maxY = Integer.MIN_VALUE;
        for (int k = 0; k < cnt[p]; k++) {
            int x = xs[p][k] + tx[p];
            int y = ys[p][k] + ty[p];
            minX = Math.min(minX, x);
            minY = Math.min(minY, y);
            maxX = Math.max(maxX, x);
            maxY = Math.max(maxY, y);
        }
        return new int[]{minX, minY, maxX, maxY};
    }

    static boolean overlaps(int p, int q) {
        int[] bp = bounds(p);
        int[] bq = bounds(q);
        if (bp[2] <= bq[0] || bq[2] <= bp[0] || bp[3] <= bq[1] || bq[3] <= bp[1]) return false;
        Area a = new Area(path(p));
        a.intersect(new Area(path(q)));
        return !a.isEmpty();
    }

    static long cross(int[] o, int[] a, int[] b) {
        return (long) (a[0] - o[0]) * (b[1] - o[1]) - (long) (a[1] - o[1]) * (b[0] - o[0]);
    }

    static boolean isRect(int mask) {
        List<int[]> pts = new ArrayList<>();
        long total = 0;
        for (int i = 0; i < n; i++) {
            if (((mask >> i) & 1) == 0) continue;
            total += area2[i];
            for (int j = 0; j < cnt[i]; j++) {
                pts.add(new int[]{xs[i][j] + tx[i], ys[i][j] + ty[i]});
            }
        }
        pts.sort((a, b) -> a[0] != b[0] ? a[0] - b[0] : a[1] - b[1]);
        int m = pts.size();
        int[][] h = new int[2 * m + 2][];
        int t = 0;
        for (int i = 0; i < m; i++) {
            while (t >= 2 && cross(h[t - 2], h[t - 1], pts.get(i)) <= 0) t--;
            h[t++] = pts.get(i);
        }
        for (int i = m - 2, lo = t + 1; i >= 0; i--) {
            while (t >= lo && cross(h[t - 2], h[t - 1], pts.get(i)) <= 0) t--;
            h[t++] = pts.get(i);
        }
        int hs = t - 1;
        if (hs != 4) return false;
        for (int i = 0; i < 4; i++) {
            int[] a = h[i], b = h[(i + 1) % 4], c = h[(i + 2) % 4];
            long dot = (long) (b[0] - a[0]) * (c[0] - b[0]) + (long) (b[1] - a[1]) * (c[1] - b[1]);
            if (dot != 0) return false;
        }
        long hullArea = 0;
        for (int i = 0; i < 4; i++) {
            int j = (i + 1) % 4;
            hullArea += (long) h[i][0] * h[j][1] - (long) h[j][0] * h[i][1];
        }
        hullArea = Math.abs(hullArea);
        return hullArea == total && hullArea > 0;
    }
}