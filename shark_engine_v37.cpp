#include <iostream>
#include <string>
#include <vector>
#include <deque>
#include <cmath>
#include <chrono>
#include <thread>
#include <mutex>
#include <atomic>
#include <unordered_map>
#include <boost/asio.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast.hpp>
#include <boost/beast/ssl.hpp>
#include "../lib/simdjson.h"
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>

using namespace std;
namespace net = boost::asio;
namespace ssl = boost::asio::ssl;
namespace beast = boost::beast;
namespace http = beast::http;
namespace websocket = beast::websocket;
using tcp = net::ip::tcp;

#pragma pack(push, 1)
struct SharedMetric {
    uint64_t sequence;
    double price, cvd_vel, abs_score, depth_ratio, ob_vel, rsi1, rsi5, rsi15, vwap_z, abs_z, rejection_z, eff_z, vwap_raw, cvd_total, total_vol_raw, oi_z, l_liq_z, s_liq_z, liq_raw, oi_raw, acc_z, score_short, score_long;
    double front_rep_z, back_shift_z, win_delta_raw; // [V61.5 FIX] High-Precision CVD
    uint64_t update_ms;
    int64_t update_time;
    int32_t signal_code, signal_side;
    double friction_val;
    char symbol[16];
    double high15m, low15m; 
};

struct SharedMemoryBlock {
    int32_t symbol_count;
    int32_t magic_number;
    char padding[56];
    SharedMetric metrics[128];
};
#pragma pack(pop)

const size_t SHM_SIZE_BYTES = sizeof(SharedMemoryBlock);
SharedMemoryBlock* shm_ptr = nullptr;
string g_shm_path = "/dev/shm/shark_shm_v60";
atomic<bool> g_reload_config{false};
atomic<bool> g_engine_running{true}; // [P0-2 FIX] single long-lived flag for pulse thread

// Network Mode
bool g_testnet = false;
string g_rest_host = "fapi.binance.com";
string g_ws_host = "fstream.binance.com";

// Configurable Constants (Defaults)
double g_score_threshold = 245.0;
double g_vwap_entry_short = 2.7;
double g_vwap_entry_long = -1.5;
double g_wall_weight_exp = 75.0;
int g_cvd_window_ms = 1500;
int g_depth_levels = 5;
double g_depth_ratio_smooth = 0.7;
double g_acc_ema_smooth = 0.5;
int g_ws_batch_size = 20;

std::mutex g_map_mutex;

void load_constants(simdjson::dom::element cfg) {
    // Read testnet flag from system section
    simdjson::dom::element sys;
    if (cfg["system"].get(sys) == simdjson::SUCCESS) {
        bool tn; if (sys["testnet"].get(tn) == simdjson::SUCCESS) g_testnet = tn;
    }
    if (g_testnet) {
        g_rest_host = "testnet.binancefuture.com";
        g_ws_host = "stream.binancefuture.com";
        printf("🧪 TESTNET MODE ACTIVE\n");
    } else {
        g_rest_host = "fapi.binance.com";
        g_ws_host = "fstream.binance.com";
    }

    simdjson::dom::element c;
    if (cfg["constants"].get(c) == simdjson::SUCCESS) {
        string_view shm; if (c["SHM_PATH"].get(shm) == simdjson::SUCCESS) g_shm_path = string(shm);
        c["SCORE_THRESHOLD_DEFAULT"].get(g_score_threshold);
        c["GATE_VWAP_SHORT"].get(g_vwap_entry_short);
        c["GATE_VWAP_LONG"].get(g_vwap_entry_long);
        c["WALL_WEIGHT_EXP"].get(g_wall_weight_exp);
        int64_t win; if (c["CVD_WINDOW_MS"].get(win) == simdjson::SUCCESS) g_cvd_window_ms = (int)win;
        int64_t d_lv; if (c["DEPTH_LEVELS"].get(d_lv) == simdjson::SUCCESS) g_depth_levels = (int)d_lv;
        c["DEPTH_RATIO_SMOOTH"].get(g_depth_ratio_smooth);
        c["ACC_EMA_SMOOTH"].get(g_acc_ema_smooth);
        int64_t b_sz; if (c["WS_BATCH_SIZE"].get(b_sz) == simdjson::SUCCESS) g_ws_batch_size = (int)b_sz;

        printf("⚙️ Constants Loaded: SHM=%s, TH=%0.1f, Win=%dms, Depth=%d, Batch=%d\n",
               g_shm_path.c_str(), g_score_threshold, g_cvd_window_ms, g_depth_levels, g_ws_batch_size);
    }
}

void signal_handler(int) { g_reload_config = true; }

double fast_atof(string_view sv) {
    if (sv.empty()) return 0.0;
    char buf[64]; size_t len = min(sv.size(), (size_t)63);
    memcpy(buf, sv.data(), len); buf[len] = '\0';
    return strtod(buf, nullptr);
}

string http_get_sync(const string& host, const string& target) {
    try {
        net::io_context ioc; ssl::context ctx{ssl::context::tlsv12_client}; ctx.set_verify_mode(ssl::verify_none);
        tcp::resolver resolver{ioc}; beast::ssl_stream<beast::tcp_stream> stream{ioc, ctx};
        if(!SSL_set_tlsext_host_name(stream.native_handle(), host.c_str())) return "";
        auto const results = resolver.resolve(host, "443");
        beast::get_lowest_layer(stream).connect(results);
        stream.handshake(ssl::stream_base::client);
        http::request<http::string_body> req{http::verb::get, target, 11};
        req.set(http::field::host, host); req.set(http::field::user_agent, "Shark-Pulse/V59.0-ULTIMATE");
        http::write(stream, req);
        beast::flat_buffer buffer; http::response<http::string_body> res; http::read(stream, buffer, res);
        return res.body();
    } catch (...) { return ""; }
}

template<typename F>
void atomic_update(SharedMetric& target, F func) {
    // [P0-3 FIX] Seqlock write protocol:
    // 1. Odd sequence signals "write in progress" — readers must retry.
    uint64_t s1 = target.sequence;
    target.sequence = s1 + 1;
    // 2. Release fence: ensures s1+1 store is globally visible BEFORE any
    //    data field writes in func(). Prevents readers from seeing new data
    //    while sequence still appears even.
    std::atomic_thread_fence(std::memory_order_release);
    func(target);
    target.update_ms = chrono::duration_cast<chrono::milliseconds>(chrono::system_clock::now().time_since_epoch()).count();
    target.update_time = time(nullptr);
    // 3. seq_cst fence: full memory barrier after ALL field writes.
    //    Upgraded from release to seq_cst to guarantee ordering on relaxed
    //    memory model architectures (e.g. ARM). Ensures every field written
    //    above is committed before the even sequence becomes visible.
    std::atomic_thread_fence(std::memory_order_seq_cst);
    target.sequence = s1 + 2;
}

class RollingStats {
    double alpha, mean = 0, m2 = 0; int count = 0;
public:
    RollingStats(double a = 0.01) : alpha(a) {}
    void update(double val) {
        if (count == 0) mean = val;
        else { double diff = val - mean; mean += alpha * diff; m2 += alpha * diff * (val - mean); }
        count++;
    }
    double get_z(double val) const {
        if (!std::isfinite(val)) return 0.0;
        double std_v = sqrt(std::max(0.0, m2));
        // [V59.2-FIX] Ultra-strict Numerical Guard
        if (std_v < 1e-7) return 0.0;
        double z = (val - mean) / std_v;
        return std::isfinite(z) ? z : 0.0;
    }
};

class RSIAnalyzer {
    deque<double> prices; double avg_gain = 0, avg_loss = 0; bool initialized = false;
    void _update_smma(double gain, double loss, bool commit) {
        if (!initialized) { 
            avg_gain += gain / 14.0; avg_loss += loss / 14.0; 
        } else {
            if (commit) {
                avg_gain = (avg_gain * 13 + gain) / 14.0;
                avg_loss = (avg_loss * 13 + loss) / 14.0;
            }
        }
    }
public:
    void seed(const vector<double>& seed_prices) {
        prices.clear(); avg_gain = 0; avg_loss = 0; initialized = false;
        if (seed_prices.size() < 15) return;
        for (size_t i = 1; i < seed_prices.size(); ++i) {
            double diff = seed_prices[i] - seed_prices[i-1];
            _update_smma(max(0.0, diff), max(0.0, -diff), true);
            prices.push_back(seed_prices[i]); if (prices.size() > 14) prices.pop_front();
            if (i == 14) initialized = true; 
        }
        initialized = true;
    }
    double update(double price, bool commit = true) {
        if (price <= 0 || prices.empty()) return 50.0;
        double diff = price - prices.back();
        double g = max(0.0, diff), l = max(0.0, -diff);
        if (commit) {
            _update_smma(g, l, true);
            prices.push_back(price); if (prices.size() > 14) prices.pop_front();
            return (avg_loss == 0) ? 100.0 : 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)));
        } else {
            double temp_gain = (avg_gain * 13 + g) / 14.0;
            double temp_loss = (avg_loss * 13 + l) / 14.0;
            return (temp_loss == 0) ? 100.0 : 100.0 - (100.0 / (1.0 + (temp_gain / temp_loss)));
        }
    }
};

class VWAPTracker {
    double total_pv = 0, total_vol = 0, mean = 0, m2 = 0;
public:
    void seed(double pv, double vol, double var_m2) { total_pv = pv; total_vol = vol; m2 = std::max(0.0, var_m2); if (vol > 0) mean = pv / vol; }
    void update(double p, double q) {
        if (p <= 0 || q <= 0 || !std::isfinite(p) || !std::isfinite(q)) return;
        double decay = (q > total_vol * 0.1) ? 0.999 : 0.9999;
        total_pv *= decay; total_vol *= decay; m2 *= decay;
        total_pv += p * q; total_vol += q;
        double diff = p - mean; mean += (q / (total_vol + 1e-9)) * diff;
        m2 += q * diff * (p - mean);
        if (!std::isfinite(m2) || m2 < 0) m2 = 0;
    }
    double get_vwap() const { return (total_vol > 0) ? total_pv / total_vol : 0; }
    double get_z(double p) const {
        if (total_vol < 1e-9 || !std::isfinite(p)) return 0;
        double std_v = sqrt(std::max(0.0, m2 / total_vol));
        if (std_v < 1e-9) return 0;
        double z = (p - get_vwap()) / std_v;
        return std::isfinite(z) ? z : 0.0;
    }
};

struct TradeItem { double delta, volume, price; uint64_t ts; };

class SymbolEngine {
public:
    std::atomic<bool> is_active{true};
    int shm_idx; string symbol;
    VWAPTracker vwap; RSIAnalyzer rsi1, rsi5, rsi15;
    RollingStats abs_st{0.02}, eff_st{0.01}, rej_st{0.01}, absorption_st{0.05}, ob_vel_st{0.05}, acc_st{0.30};
    deque<TradeItem> cvd_window;
    double current_cvd = 0, win_delta = 0, win_vol = 0, smooth_dr = 0.0, last_cvd_vel = 0.0, last_acc_ema = 0.0;
    RollingStats front_rep_st{0.05}, back_shift_st{0.05};
    double prev_bid_front = 0, prev_bid_back = 0, prev_ask_front = 0, prev_ask_back = 0;
    double trade_sell_vol_100ms = 0, trade_buy_vol_100ms = 0;

    SymbolEngine(int idx, const string& s) : shm_idx(idx), symbol(s) { acc_st = RollingStats(0.15); }

    void seed_history() {
        auto fetch_candles_raw = [&](const string& interval, int limit) -> string {
            if (!is_active) return "";
            string target = "/fapi/v1/klines?symbol=" + symbol + "&interval=" + interval + "&limit=" + to_string(limit);
            return http_get_sync(g_rest_host, target);
        };

        string b1 = fetch_candles_raw("1m", 500);
        string b5 = fetch_candles_raw("5m", 500);
        string b15 = fetch_candles_raw("15m", 500);

        simdjson::dom::parser p;
        simdjson::dom::array a1, a5, a15;

        // Process 1m (Price, RSI1, VWAP)
        if (p.parse(b1).get(a1) == simdjson::SUCCESS && a1.size() > 20) {
            vector<double> prices; double acc_pv = 0, acc_vol = 0;
            for (auto item : a1) {
                double c = fast_atof(item.at(4).get_string().value());
                double v = fast_atof(item.at(5).get_string().value());
                double qv = fast_atof(item.at(7).get_string().value());
                if (c > 0) { prices.push_back(c); acc_pv += qv; acc_vol += v; }
            }
            double last_p = prices.back();
            vector<double> subset(prices.begin(), prices.end() - 1);
            rsi1.seed(subset); 
            double m_mean = acc_pv / (acc_vol + 1e-9); double acc_m2 = 0;
            for(auto& pv : prices) { double df = pv - m_mean; acc_m2 += (acc_vol/prices.size()) * df * df; }
            vwap.seed(acc_pv, acc_vol, acc_m2);
            
            if (!is_active) return;
            atomic_update(shm_ptr->metrics[shm_idx], [&](SharedMetric& m) {
                m.price = last_p;
                m.rsi1 = rsi1.update(last_p);
                m.vwap_raw = vwap.get_vwap();
                m.vwap_z = vwap.get_z(last_p);
            });
        }

        // Process 5m (RSI5)
        if (p.parse(b5).get(a5) == simdjson::SUCCESS && a5.size() > 20) {
            vector<double> prices;
            for (auto item : a5) {
                double c = fast_atof(item.at(4).get_string().value());
                if (c > 0) prices.push_back(c);
            }
            vector<double> subset(prices.begin(), prices.end() - 1);
            rsi5.seed(subset);
            double r5 = rsi5.update(prices.back());
            if (!is_active) return;
            atomic_update(shm_ptr->metrics[shm_idx], [&](SharedMetric& m) { m.rsi5 = r5; });
        }

        // Process 15m (RSI15, Terrain)
        if (p.parse(b15).get(a15) == simdjson::SUCCESS && a15.size() > 20) {
            vector<double> prices;
            for (auto item : a15) {
                double c = fast_atof(item.at(4).get_string().value());
                if (c > 0) prices.push_back(c);
            }
            vector<double> subset(prices.begin(), prices.end() - 1);
            rsi15.seed(subset);
            
            // Previous candle for Terrain
            auto prev_k = a15.at(a15.size() - 2);
            double prev_h = fast_atof(prev_k.at(2).get_string().value());
            double prev_l = fast_atof(prev_k.at(3).get_string().value());

            double r15 = rsi15.update(prices.back());
            if (!is_active) return;
            atomic_update(shm_ptr->metrics[shm_idx], [&](SharedMetric& m) { 
                m.rsi15 = r15; 
                if (prev_h > 0) m.high15m = prev_h;
                if (prev_l > 0) m.low15m = prev_l;
            });
            cerr << "✅ Seeded: " << symbol << " (" << shm_idx + 1 << "/100)" << endl;
        }
    }

    void on_trade(double p, double q, bool is_sell) {
        if (!is_active) return;
        
        if (shm_idx >= 0 && shm_idx < 128) {
             char shm_sym[16];
             strncpy(shm_sym, shm_ptr->metrics[shm_idx].symbol, 15); shm_sym[15] = '\0';
             if (symbol != shm_sym) return;
        }

        double val = p * q, delta = is_sell ? -val : val;
        if (is_sell) trade_sell_vol_100ms += val;
        else trade_buy_vol_100ms += val;
        uint64_t now_ms = chrono::duration_cast<chrono::milliseconds>(chrono::system_clock::now().time_since_epoch()).count();
        cvd_window.push_back({delta, val, p, now_ms}); win_delta += delta; win_vol += val;
        // [V58.6-FIX] Use configurable window for Micro-Absorption
        while (!cvd_window.empty() && now_ms - cvd_window.front().ts > g_cvd_window_ms) { win_delta -= cvd_window.front().delta; win_vol -= cvd_window.front().volume; cvd_window.pop_front(); }
        double p_diff = cvd_window.empty() ? 0 : abs(p - cvd_window.front().price);
        double eff = (win_vol > 0) ? (p_diff / (abs(win_delta) + 1.0)) * 1e6 : 0.0;
        double absorption = (win_vol > 0) ? (win_vol / (p_diff + (p * 1e-4))) : 0.0;
        current_cvd += delta; vwap.update(p, q);
        double current_vel = tanh(win_delta / (win_vol + 1.0) * 2.0), delta_v = current_vel - last_cvd_vel;
        last_acc_ema = (delta_v * g_acc_ema_smooth) + (last_acc_ema * (1.0 - g_acc_ema_smooth)); last_cvd_vel = current_vel;
        abs_st.update(win_vol); eff_st.update(eff); absorption_st.update(absorption); acc_st.update(last_acc_ema);
        double cur_fric = absorption_st.get_z(absorption);
        
        // [V58.0 Patch] RSI Duality Removal: C++ no longer calculates real-time RSI.
        // Python is the single source of truth for RSI to match Binance candle closes.

        atomic_update(shm_ptr->metrics[shm_idx], [&](SharedMetric& m) {
            m.price = p; m.cvd_total = current_cvd; m.vwap_raw = vwap.get_vwap(); m.cvd_vel = current_vel; m.acc_z = acc_st.get_z(last_acc_ema);
            m.vwap_z = vwap.get_z(p); m.abs_z = abs_st.get_z(win_vol); m.eff_z = eff_st.get_z(eff);
            m.abs_score = absorption_st.get_z(absorption); m.total_vol_raw = win_vol;
            m.friction_val = cur_fric;
            // RSI fields are now managed exclusively by Python
        });
    }

    void on_depth(simdjson::dom::array bids, simdjson::dom::array asks) {
        if (!is_active) return;
        double s_bid = 0, s_ask = 0, bid_front = 0, bid_back = 0, ask_front = 0, ask_back = 0; 
        int count = 0;
        
        for(auto b : bids) { 
            string_view p_str, q_str;
            if (b.at(0).get(p_str) == simdjson::SUCCESS && b.at(1).get(q_str) == simdjson::SUCCESS) {
                double vol = fast_atof(p_str) * fast_atof(q_str);
                s_bid += vol;
                if (count < 4) bid_front += vol;
                else bid_back += vol;
            }
            if(++count >= g_depth_levels) break; 
        }
        count = 0; 
        for(auto a : asks) { 
            string_view p_str, q_str;
            if (a.at(0).get(p_str) == simdjson::SUCCESS && a.at(1).get(q_str) == simdjson::SUCCESS) {
                double vol = fast_atof(p_str) * fast_atof(q_str);
                s_ask += vol;
                if (count < 4) ask_front += vol;
                else ask_back += vol;
            }
            if(++count >= g_depth_levels) break; 
        }
        
        double bid_rep = (bid_front - prev_bid_front) + trade_sell_vol_100ms;
        double ask_rep = (ask_front - prev_ask_front) + trade_buy_vol_100ms;
        double net_front_rep = bid_rep - ask_rep;
        
        double bid_shift = bid_back - prev_bid_back;
        double ask_shift = ask_back - prev_ask_back;
        double net_back_shift = bid_shift - ask_shift;
        
        front_rep_st.update(net_front_rep);
        back_shift_st.update(net_back_shift);
        
        prev_bid_front = bid_front; prev_bid_back = bid_back;
        prev_ask_front = ask_front; prev_ask_back = ask_back;
        trade_sell_vol_100ms = 0; trade_buy_vol_100ms = 0;

        double raw_ratio = (s_bid - s_ask) / (s_bid + s_ask + 1.0);
        smooth_dr = (smooth_dr * g_depth_ratio_smooth) + (raw_ratio * (1.0 - g_depth_ratio_smooth)); 
        atomic_update(shm_ptr->metrics[shm_idx], [&](SharedMetric& m) { 
            m.depth_ratio = smooth_dr; 
            m.liq_raw = s_bid + s_ask; 
            m.front_rep_z = front_rep_st.get_z(net_front_rep);
            m.back_shift_z = back_shift_st.get_z(net_back_shift);
            m.win_delta_raw = win_delta; // [V61.5 FIX] Pass raw CVD imbalance
        });
    }

    void on_rsi(int type, double val, double high = 0, double low = 0) {
        if (!is_active) return;
        // [V58.0 Patch] RSI Duality Removal: No SHM updates from C++ kline events.
        // Python handles all RSI logic now.
    }
};

vector<shared_ptr<SymbolEngine>> engines(128, nullptr);
unordered_map<string, int> g_sym_map;

class session : public enable_shared_from_this<session> {
    tcp::resolver resolver_; websocket::stream<beast::ssl_stream<beast::tcp_stream>> ws_;
    beast::flat_buffer buffer_; string host_, target_; simdjson::dom::parser parser_;
public:
    explicit session(net::io_context& ioc, ssl::context& ctx) : resolver_(ioc), ws_(ioc, ctx) {}
    void run(char const* host, string const& target) { host_ = host; target_ = target; resolver_.async_resolve(host, "443", beast::bind_front_handler(&session::on_resolve, shared_this())); }
    void on_resolve(beast::error_code ec, tcp::resolver::results_type res) { if(!ec) beast::get_lowest_layer(ws_).async_connect(res, beast::bind_front_handler(&session::on_connect, shared_this())); }
    void on_connect(beast::error_code ec, tcp::resolver::results_type::endpoint_type) { if(!ec) { if(!SSL_set_tlsext_host_name(ws_.next_layer().native_handle(), host_.c_str())) return; ws_.next_layer().async_handshake(ssl::stream_base::client, beast::bind_front_handler(&session::on_ssl, shared_this())); } }
    void on_ssl(beast::error_code ec) { if(!ec) ws_.async_handshake(host_, target_, beast::bind_front_handler(&session::on_handshake, shared_this())); }
    void on_handshake(beast::error_code ec) { if(!ec) do_read(); }
    void do_read() { ws_.async_read(buffer_, beast::bind_front_handler(&session::on_read, shared_this())); }
    void on_read(beast::error_code ec, size_t bt) {
        static atomic<int> parse_errors{0};
        if (ec) return;
        if (g_reload_config) { ws_.async_close(websocket::close_code::normal, [](beast::error_code){}); return; }
        try {
            simdjson::dom::element doc;
            simdjson::padded_string padded_json((const char*)buffer_.data().data(), buffer_.data().size());
            if (parser_.parse(padded_json).get(doc) == simdjson::SUCCESS) {
                simdjson::dom::element d; if (doc["data"].get(d) == simdjson::SUCCESS) {
                    string_view sym; d["s"].get(sym); 
                    std::lock_guard<std::mutex> lock(g_map_mutex);
                    auto it = g_sym_map.find(string(sym));
                    if (it != g_sym_map.end()) {
                        int s_idx = it->second;
                        if (s_idx >= 0 && s_idx < 128 && engines[s_idx] != nullptr) {
                            string_view ev; d["e"].get(ev);
                            if (ev == "trade") engines[s_idx]->on_trade(fast_atof(d["p"].get_string().value()), fast_atof(d["q"].get_string().value()), d["m"].get_bool());
                            else if (ev == "depthUpdate") engines[s_idx]->on_depth(d["b"].get_array(), d["a"].get_array());
                            else if (ev == "kline") {
                                // [V58.0 Patch] RSI Duality Removal: No kline processing needed in C++
                                // Python handles RSI calibration via REST polling.
                            }
                        }
                    }
                }
            }
        } catch(...) {
            if (++parse_errors % 1000 == 0) cerr << "⚠️ Parse errors: " << parse_errors << endl;
        }
        buffer_.consume(bt); do_read();
    }
    shared_ptr<session> shared_this() { return shared_from_this(); }
};

int main(int argc, char** argv) {
    if (argc < 2) return 1;
    ios_base::sync_with_stdio(false); cin.tie(NULL);
    cout << unitbuf; // Force real-time log flushing
    string cfg_path = argv[1]; signal(SIGUSR1, signal_handler);
    int fd = open(g_shm_path.c_str(), O_CREAT | O_RDWR, 0666);
    if (fd < 0) {
        perror("❌ CRITICAL: Failed to open SHM file");
        return 1;
    }
    if (ftruncate(fd, SHM_SIZE_BYTES) < 0) {
        perror("❌ CRITICAL: Failed to truncate SHM file");
        return 1;
    }
    printf("✅ SHM File Ready: %s (Size: %zu bytes)\n", g_shm_path.c_str(), SHM_SIZE_BYTES);
    fflush(stdout);
    shm_ptr = (SharedMemoryBlock*)mmap(0, SHM_SIZE_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (shm_ptr == MAP_FAILED) {
        perror("❌ CRITICAL: MMAP Failed");
        return 1;
    }

    // [V60.0 FIX] Cold Boot Protocol: Zero-fill SHM to prevent Ghost Data
    memset(shm_ptr, 0, SHM_SIZE_BYTES);
    printf("❄️ Cold Boot: Shared Memory Zero-filled (Clean Slate).\n");
    fflush(stdout);

    shm_ptr->magic_number = 380;
    unordered_map<string, shared_ptr<SymbolEngine>> active_engines;

    // Force initial load
    g_reload_config = true;

    // [P0-2 FIX] Single long-lived pulse thread created ONCE here.
    // Previously created inside the reload loop and detached on every config
    // reload, causing unbounded thread accumulation (thread exhaustion leak).
    thread pulse_thread([]() {
        while (g_engine_running.load()) {
            this_thread::sleep_for(chrono::seconds(10));
        }
    });

    while (true) {
        printf("DEBUG: Configuration reload triggered. Mode: HOT-SWAP\n");
        fflush(stdout);
        g_reload_config = false; 
        simdjson::dom::parser p; simdjson::dom::element cfg;
        if (p.load(cfg_path).get(cfg) != simdjson::SUCCESS) { sleep(2); continue; }
        
        // [V60.7] Update configurable engine constants
        load_constants(cfg);
        
        simdjson::dom::object syms = cfg["symbols"].get_object(); 
        unordered_map<string, shared_ptr<SymbolEngine>> next_engines;
        int idx = 0;
        
        {
            std::lock_guard<std::mutex> lock(g_map_mutex);
            g_sym_map.clear();
 
    
            // [V56.8 HOT-SWAP] Identify and Preserve Existing Engines
            for (auto field : syms) {
                string s = string(field.key);
                if (active_engines.count(s)) {
                    // Preserve existing instance to keep RollingStats/VWAP alive
                    next_engines[s] = active_engines[s];
                    next_engines[s]->shm_idx = idx; // Update SHM index
                    active_engines.erase(s);
                    
                    // [V58.0 Patch] Preserve SHM Data for Hot-Swap
                    strncpy(shm_ptr->metrics[idx].symbol, s.c_str(), 15);
                } else {
                    // New symbol detected
                    next_engines[s] = make_shared<SymbolEngine>(idx, s);
                    printf("🆕 New Symbol Detected: %s\n", s.c_str());
                    
                    // [V58.0 Patch] Only Zero-out NEW symbols
                    memset(&shm_ptr->metrics[idx], 0, sizeof(SharedMetric)); 
                    strncpy(shm_ptr->metrics[idx].symbol, s.c_str(), 15);
                }
                
                engines[idx] = next_engines[s];
                g_sym_map[s] = idx;
                idx++; if (idx >= 128) break;
            }
    
            // Cleanup removed symbols
            for (auto& pair : active_engines) {
                printf("🗑️ Removing Symbol: %s\n", pair.first.c_str());
                pair.second->is_active = false;
            }
            for (int i = idx; i < 128; ++i) engines[i] = nullptr;
            active_engines = next_engines;
            shm_ptr->symbol_count = idx;
        }
        
        // Identify fresh engines for seeding
        vector<shared_ptr<SymbolEngine>> new_seeds;
        for (auto& pair : active_engines) {
            if (pair.second->cvd_window.empty()) new_seeds.push_back(pair.second);
        }

        if (!new_seeds.empty()) {
            cerr << "🚀 Seeding " << new_seeds.size() << " NEW symbols in background..." << endl;
            std::thread seed_thread([new_seeds]() {
                for (auto e : new_seeds) { 
                    e->seed_history(); 
                    this_thread::sleep_for(chrono::milliseconds(50));
                }
            });
            seed_thread.detach();
        }
        
        net::io_context ioc; ssl::context ctx{ssl::context::tlsv12_client};
        int batch_size = g_ws_batch_size; 
        for (int i = 0; i < idx; i += batch_size) {
            string batch_path = "/stream?streams=";
            for (int j = i; j < min(i + batch_size, idx); ++j) {
                string s = engines[j]->symbol; string ls = s; for(auto &c : ls) c = tolower(c);
                batch_path += (j == i ? "" : "/") + ls + "@trade/" + ls + "@depth10@100ms";
            }
            make_shared<session>(ioc, ctx)->run(g_ws_host.c_str(), batch_path);
        }
        printf("🔓 IO Loop Started. Active: %d Symbols.\n", idx);
        fflush(stdout);
        ioc.run();
        if (!g_reload_config) break;
    }
    // [P0-2 FIX] Signal pulse thread to exit and join cleanly
    g_engine_running = false;
    pulse_thread.join();
    return 0;
}
