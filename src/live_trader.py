import time
import pandas as pd
import numpy as np
import re
import json
import logging
from datetime import datetime, timedelta
import pytz

try:
    import MetaTrader5 as mt5
except ImportError:
    from . import mock_metatrader5 as mt5

from .data_collector import MT5DataCollector, EconomicCalendarCollector
from .agents.data_analyst_agent import DataAnalystAgent
from .agents.market_researcher_agent import MarketResearcherAgent
from .agents.trader_agent import TraderAgent
from .agents.risk_manager_agent import RiskManagerAgent
from .agents.fund_manager_agent import FundManagerAgent
from .ufo_calculator import UfoCalculator
from .llm.llm_client import LLMClient
from . import utils
from .trade_executor import TradeExecutor
from .portfolio_manager import PortfolioManager
from .ufo_trading_engine import UFOTradingEngine
from .dynamic_reinforcement_engine import DynamicReinforcementEngine

class LiveTrader:
    def __init__(self, config):
        self.config = config
        self._setup_logging()
        self._parse_config()

        self.llm_client = LLMClient(api_key=config['openrouter']['api_key'])
        self.mt5_collector = MT5DataCollector(
            login=config['mt5']['login'],
            password=config['mt5']['password'],
            server=config['mt5']['server'],
            path=config['mt5']['path']
        )
        self.trade_executor = TradeExecutor(self.mt5_collector, self.config)
        self.ufo_calculator = UfoCalculator(config['trading']['currencies'].split(','))

        # PortfolioManager is kept for account info but not primary position management
        self.portfolio_manager = PortfolioManager(self.mt5_collector, self.trade_executor, self.config)
        self.ufo_engine = UFOTradingEngine(config, self.ufo_calculator)

        self.agents = {
            "data_analyst": DataAnalystAgent("DataAnalyst", self.mt5_collector),
            "researcher": MarketResearcherAgent("MarketResearcher", self.llm_client),
            "trader": TraderAgent("Trader", self.llm_client, self.mt5_collector, symbols=self.config['trading']['symbols'].split(',')),
            "risk_manager": RiskManagerAgent("RiskManager", self.llm_client, self.mt5_collector, self.config),
            "fund_manager": FundManagerAgent("FundManager", self.llm_client)
        }

        self.dynamic_reinforcement_engine = DynamicReinforcementEngine(config)
        if self.dynamic_reinforcement_engine.enabled:
            logging.info("✅ Dynamic Reinforcement Engine enabled")
        else:
            logging.warning("⚠️ Dynamic Reinforcement Engine disabled")

        # State variables aligned with simulation
        self.portfolio_value = 0.0
        self.initial_balance = 0.0
        self.realized_pnl = 0.0
        self.last_cycle_time = 0
        self.cycle_count = 0
        self.open_positions = []  # Primary source of truth for positions, like the simulator
        self.closed_trades = []
        self.trades_executed = []
        self.previous_ufo_data = None
        self.last_monitoring_time = 0
        self._position_peaks = {} # For trailing stops

        self._initialize_portfolio()

    def _parse_config(self):
        """Parses all configuration values from config.ini, mirroring the simulator."""
        self.cycle_period_minutes = utils.get_config_value(self.config, 'trading', 'cycle_period_minutes', 30)
        self.cycle_period_seconds = self.cycle_period_minutes * 60
        self.position_update_frequency_minutes = utils.get_config_value(self.config, 'trading', 'position_update_frequency_minutes', 5)
        self.position_update_frequency_seconds = self.position_update_frequency_minutes * 60
        self.continuous_monitoring_enabled = utils.get_config_value(self.config, 'trading', 'continuous_monitoring_enabled', True)

        # Diversification
        self.max_concurrent_positions = utils.get_config_value(self.config, 'trading', 'max_concurrent_positions', 18)
        self.target_positions_when_available = utils.get_config_value(self.config, 'trading', 'target_positions_when_available', 6)
        self.min_positions_for_session = utils.get_config_value(self.config, 'trading', 'min_positions_for_session', 4)

        # Position Management Rules from Simulator
        self.profit_target = utils.get_config_value(self.config, 'trading', 'profit_target', 75.0)
        self.stop_loss = utils.get_config_value(self.config, 'trading', 'stop_loss', -50.0)
        self.max_position_duration_hours = utils.get_config_value(self.config, 'trading', 'max_position_duration_hours', 4)
        self.enable_trailing_stop = utils.get_config_value(self.config, 'trading', 'enable_trailing_stop', True)
        self.trailing_stop_trigger_pnl = utils.get_config_value(self.config, 'trading', 'trailing_stop_trigger_pnl', 30.0)
        self.trailing_stop_distance_pnl = utils.get_config_value(self.config, 'trading', 'trailing_stop_distance_pnl', 15.0)
        logging.info("✅ All configuration parameters parsed.")

    def _setup_logging(self):
        file_handler = logging.FileHandler("live_trader.log", encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        if root_logger.hasHandlers():
            root_logger.handlers.clear()
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

    def _initialize_portfolio(self):
        """Initializes portfolio by fetching account info and syncing open positions."""
        if self.mt5_collector.connect():
            account_info = self.portfolio_manager.get_account_info()
            if account_info:
                self.initial_balance = account_info.balance
                self.portfolio_value = account_info.equity
                logging.info(f"✅ Portfolio initialized. Initial Balance: ${self.initial_balance:,.2f}, Equity: ${self.portfolio_value:,.2f}")

                # Sync open positions from broker to our internal state
                self.sync_positions_from_broker()
            else:
                logging.error("⚠️ Could not retrieve account info. Cannot start.")
                raise ConnectionError("Failed to retrieve MT5 account info on startup.")
            self.mt5_collector.disconnect()
        else:
            logging.error("⚠️ MT5 connection failed during portfolio initialization.")
            raise ConnectionError("Failed to connect to MT5 on startup.")

    def sync_positions_from_broker(self):
        """Syncs the internal `self.open_positions` list with the broker's current positions."""
        logging.info("Syncing positions from broker...")
        broker_positions = self.portfolio_manager.get_positions()
        if broker_positions is None:
            logging.warning("Could not get positions from broker. Assuming no open positions.")
            self.open_positions = []
            return

        synced_positions = []
        for _, pos in broker_positions.iterrows():
            synced_pos = {
                'ticket': pos.ticket,
                'symbol': pos.symbol,
                'direction': 'BUY' if pos.type == 0 else 'SELL',
                'volume': pos.volume,
                'entry_price': pos.price_open,
                'pnl': pos.profit,
                'timestamp': pd.to_datetime(pos.time, unit='s'),
                'comment': pos.comment,
                'peak_pnl': pos.profit # Initialize peak P&L
            }
            synced_positions.append(synced_pos)
            if pos.ticket not in self._position_peaks:
                self._position_peaks[pos.ticket] = pos.profit

        self.open_positions = synced_positions
        logging.info(f"✅ Synced {len(self.open_positions)} positions from broker.")

    def run(self):
        """Main trading loop, orchestrating the main cycle and continuous monitoring."""
        logging.info(f"🚀 Starting Live Trading")
        logging.info(f"⏰ Cycle Frequency: Every {self.cycle_period_minutes} minutes")
        if self.continuous_monitoring_enabled:
            logging.info(f"📊 Continuous Monitoring: Position updates every {self.position_update_frequency_minutes} minutes")

        self.last_cycle_time = time.time() - self.cycle_period_seconds - 1

        try:
            while True:
                now = time.time()

                if self.continuous_monitoring_enabled:
                    if now - self.last_monitoring_time >= self.position_update_frequency_seconds:
                        self.continuous_position_monitoring()
                        self.last_monitoring_time = now

                if now - self.last_cycle_time >= self.cycle_period_seconds:
                    if self.check_session_status():
                        self.run_main_trading_cycle()
                    else:
                        logging.info(f"({datetime.now().strftime('%H:%M:%S')}) Outside active trading session. Skipping main cycle.")
                    self.last_cycle_time = now

                time_to_next_event = min(
                    (self.last_cycle_time + self.cycle_period_seconds) - now,
                    (self.last_monitoring_time + self.position_update_frequency_seconds) - now if self.continuous_monitoring_enabled else float('inf')
                )
                time.sleep(max(1, time_to_next_event))

        except KeyboardInterrupt:
            logging.info("\nTrading interrupted by user. Exiting...")
        except Exception as e:
            logging.critical(f"CRITICAL ERROR in main loop: {e}", exc_info=True)
            time.sleep(60)
        finally:
            self.generate_final_summary()

    def run_main_trading_cycle(self):
        """Runs the main 10-phase agentic workflow, mirroring the simulation."""
        self.cycle_count += 1
        logging.info("\n" + "="*60)
        logging.info(f"🚀 CYCLE {self.cycle_count} - {datetime.now().strftime('%H:%M:%S')} GMT")
        logging.info("="*60)

        # Sync positions at the start of each cycle to ensure data is fresh
        self.sync_positions_from_broker()

        # PHASE 1: Data Collection
        logging.info("📊 PHASE 1: Data Collection")
        price_data = self.collect_market_data()
        if not price_data: return

        # PHASE 2: UFO Analysis
        logging.info("🛸 PHASE 2: UFO Analysis")
        ufo_data = self.calculate_ufo_indicators(price_data)
        if not ufo_data: return

        # PHASE 3: Economic Calendar
        logging.info("📅 PHASE 3: Economic Calendar")
        economic_events = self.agents['data_analyst'].execute({'source': 'economic_calendar'})

        # PHASE 4: Market Research
        logging.info("🔍 PHASE 4: Market Research")
        research_result = self.agents['researcher'].execute(ufo_data, economic_events)

        # PHASE 5: UFO Portfolio Management (Priority Checks)
        logging.info("💼 PHASE 5: UFO Portfolio Management")
        if self.perform_priority_portfolio_checks(ufo_data, economic_events):
            return # A critical action was taken (e.g., portfolio stop), so end cycle.

        # Store UFO data for next cycle comparison
        if ufo_data:
            self.previous_ufo_data = ufo_data

        # PHASE 6: Trading Decisions
        logging.info("🎯 PHASE 6: Trading Decisions")
        current_positions_df = pd.DataFrame(self.open_positions) if self.open_positions else pd.DataFrame()
        trade_decisions = self.generate_trade_decisions(research_result, current_positions_df)

        # PHASE 7: Risk Assessment
        logging.info("⚖️ PHASE 7: Risk Assessment")
        risk_assessment = self.agents['risk_manager'].execute(trade_decisions)

        # PHASE 8: Fund Manager Authorization
        logging.info("💰 PHASE 8: Fund Manager Authorization")
        authorization = self.agents['fund_manager'].execute(trade_decisions, risk_assessment)

        # PHASE 9: Trade Execution
        logging.info("⚡ PHASE 9: Trade Execution")
        self.execute_approved_trades(authorization, trade_decisions, current_positions_df, ufo_data)

        # PHASE 10: Cycle Summary
        logging.info("📋 PHASE 10: Cycle Summary")
        self.generate_cycle_summary()

    def perform_priority_portfolio_checks(self, ufo_data, economic_events):
        """Performs high-priority checks from the simulation's Phase 5."""
        # 1. Check for portfolio-level equity stop
        account_info = self.portfolio_manager.get_account_info()
        if account_info:
            is_breached, reason = self.ufo_engine.check_portfolio_equity_stop(account_info.balance, account_info.equity)
            if is_breached:
                logging.critical(f"🚨 UFO PORTFOLIO STOP TRIGGERED: {reason}")
                logging.critical("🚨 Closing ALL positions immediately!")
                for pos in self.open_positions:
                    self.trade_executor.close_trade(pos['ticket'])
                self.sync_positions_from_broker() # Resync after closing
                return True # Stop the cycle

        # 2. Check for session end
        should_close, reason = self.ufo_engine.should_close_for_session_end(economic_events)
        if should_close:
            logging.info(f"🌅 UFO SESSION END: {reason}. Closing all positions.")
            for pos in self.open_positions:
                self.trade_executor.close_trade(pos['ticket'])
            self.sync_positions_from_broker()
            return True

        # 3. Analyze for UFO exit signals
        if self.previous_ufo_data:
            exit_signals = self.analyze_ufo_exit_signals(ufo_data, self.previous_ufo_data)
            if exit_signals:
                logging.info(f"📈 UFO Exit Signals detected: {len(exit_signals)} currency changes")
                if len(exit_signals) >= 3:
                    logging.warning("🚨 STRONG EXIT SIGNALS detected: Auto-closing affected positions.")
                    self.close_affected_positions(exit_signals)

        return False

    def continuous_position_monitoring(self):
        """High-frequency monitoring of open positions, mirroring simulator's logic."""
        logging.info(f"--- Continuous Position Monitoring ({datetime.now().strftime('%H:%M:%S')}) ---")

        # 1. Sync positions and get latest account info
        self.sync_positions_from_broker()
        account_info = self.portfolio_manager.get_account_info()
        if not account_info:
            logging.warning("Could not get account info for monitoring.")
            return

        # 2. Manage each position individually (TP, SL, Trailing Stop, Time)
        positions_to_close = []
        for pos in self.open_positions:
            close_reason = self._check_individual_position_rules(pos)
            if close_reason:
                positions_to_close.append((pos, close_reason))

        for pos, reason in positions_to_close:
            logging.info(f"🎯 Auto-closing position {pos['ticket']} ({pos['symbol']}): {reason} (P&L: ${pos['pnl']:.2f})")
            if self.trade_executor.close_trade(pos['ticket']):
                # Record closed trade immediately
                closed_trade_info = pos.copy()
                closed_trade_info['close_reason'] = reason
                closed_trade_info['close_time'] = datetime.now()
                self.closed_trades.append(closed_trade_info)
                self.realized_pnl += pos['pnl']

        if positions_to_close:
            self.sync_positions_from_broker() # Resync after closing

        # 3. Dynamic Reinforcement Check
        if self.dynamic_reinforcement_engine.enabled:
            self.check_and_execute_dynamic_reinforcement()

        # 4. Update and log portfolio status
        self.portfolio_value = account_info.equity
        unrealized_pnl = sum(p['pnl'] for p in self.open_positions)
        logging.info(f"💰 Portfolio Value: ${self.portfolio_value:,.2f} | Open Positions: {len(self.open_positions)} | Unrealized P&L: ${unrealized_pnl:,.2f}")
        logging.info("--- End of Monitoring ---")

    def _check_individual_position_rules(self, position):
        """Checks a single position against profit, loss, time, and trailing stop rules."""
        # Update peak P&L for trailing stop
        ticket = position['ticket']
        current_pnl = position['pnl']

        if ticket not in self._position_peaks:
            self._position_peaks[ticket] = current_pnl
        else:
            self._position_peaks[ticket] = max(self._position_peaks[ticket], current_pnl)

        peak_pnl = self._position_peaks[ticket]

        # Rule 1: Profit Target
        if current_pnl >= self.profit_target:
            return "profit_target"

        # Rule 2: Stop Loss
        if current_pnl <= self.stop_loss:
            return "stop_loss"

        # Rule 3: Time-based Exit
        position_age = datetime.now(pytz.utc) - position['timestamp'].astimezone(pytz.utc)
        if position_age.total_seconds() > self.max_position_duration_hours * 3600:
            return "time_based_exit"

        # Rule 4: Trailing Stop
        if self.enable_trailing_stop:
            if peak_pnl >= self.trailing_stop_trigger_pnl:
                trailing_stop_level = peak_pnl - self.trailing_stop_distance_pnl
                if current_pnl <= trailing_stop_level:
                    return "trailing_stop"

        return None

    def execute_approved_trades(self, authorization, trade_decisions, current_positions, ufo_data):
        """Executes trades if approved by the fund manager and UFO engine."""
        if "APPROVE" not in authorization.upper():
            logging.info("❌ Trades not approved by Fund Manager - No execution.")
            return

        # Check UFO engine conditions for opening new trades
        should_trade, reason = self.ufo_engine.should_open_new_trades(
            current_positions=current_positions,
            portfolio_status={'balance': self.initial_balance, 'equity': self.portfolio_value},
            ufo_data=ufo_data
        )
        if not should_trade:
            logging.warning(f"❌ UFO Engine blocked trades: {reason}")
            return
        
        logging.info(f"✅ UFO Engine approved opening new trades: {reason}")

        try:
            parsed_trades = self.parse_trade_decisions(trade_decisions)
            for trade in parsed_trades:
                if trade.get('action') == 'new_trade':
                    self._execute_new_trade(trade, ufo_data)
        except Exception as e:
            logging.error(f"❌ Trade execution error: {e}", exc_info=True)

    def _execute_new_trade(self, trade_details, ufo_data):
        """Handles the logic for executing a single new trade."""
        symbol = trade_details.get('symbol') or trade_details.get('currency_pair', '')
        direction = trade_details.get('direction', '').upper()
        volume = trade_details.get('volume') or trade_details.get('lot_size', 0.1)

        # Validate and correct currency pair format
        corrected_symbol, corrected_direction = self.ufo_engine.validate_and_correct_currency_pair(symbol, direction)
        if not corrected_symbol:
            logging.error(f"⚠️ Skipping invalid or uncorrectable currency pair: {symbol}")
            return

        if direction != corrected_direction:
            logging.warning(f"⚠️ Direction inverted due to pair correction: {direction} -> {corrected_direction}")
            direction = corrected_direction

        # Add suffix if needed
        suffix = self.config['mt5'].get('symbol_suffix', '')
        if not corrected_symbol.endswith(suffix):
            full_symbol = corrected_symbol + suffix
        else:
            full_symbol = corrected_symbol
            
        # Calculate optimal entry price using UFO methodology
        optimal_price = self.calculate_ufo_entry_price(full_symbol, direction, ufo_data)

        # Execute the trade
        trade_type = mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL
        comment = f"UFO_Cycle_{self.cycle_count}"
        
        result = self.trade_executor.execute_ufo_trade(
            symbol=full_symbol,
            trade_type=trade_type,
            volume=volume,
            price=optimal_price, # Pass the optimal price
            comment=comment
        )

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"✅ Trade executed: {full_symbol} {direction} {volume} lots. Ticket: {result.order}")
            # Immediately sync to update internal state
            self.sync_positions_from_broker()
        else:
            logging.error(f"❌ Trade execution failed for {full_symbol}. Reason: {result.comment if result else 'Unknown'}")

    # Helper methods (data collection, ufo calc, etc.) are refactored versions of the originals
    # to fit the new structure. They are largely similar to the simulator's implementation.
    
    def collect_market_data(self):
        """Collects market data for all configured symbols."""
        try:
            symbols = self.config['trading']['symbols'].split(',')
            all_data = {}
            timeframes = [mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15, mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, mt5.TIMEFRAME_D1]
            timeframe_bars = {
                mt5.TIMEFRAME_M5: 240, mt5.TIMEFRAME_M15: 80, mt5.TIMEFRAME_H1: 20,
                mt5.TIMEFRAME_H4: 120, mt5.TIMEFRAME_D1: 100
            }

            for symbol in symbols:
                data = self.agents['data_analyst'].execute({
                    'source': 'mt5', 'symbol': symbol, 'timeframes': timeframes, 'num_bars': timeframe_bars
                })
                all_data[symbol] = data
            
            logging.info(f"✅ Collected data for {len(all_data)} symbols")
            return all_data
        except Exception as e:
            logging.error(f"❌ Data collection error: {e}", exc_info=True)
            return None

    def calculate_ufo_indicators(self, price_data):
        """Calculates UFO indicators with enhanced analysis."""
        try:
            reshaped_data = {}
            for symbol, timeframe_data in price_data.items():
                for timeframe, df in timeframe_data.items():
                    if df is not None and not df.empty:
                        if timeframe not in reshaped_data:
                            reshaped_data[timeframe] = pd.DataFrame()
                        reshaped_data[timeframe][symbol] = df['close']

            if not reshaped_data:
                logging.error("No valid data to calculate UFO indicators.")
                return None

            incremental_sums = {tf: self.ufo_calculator.calculate_incremental_sum(
                                    self.ufo_calculator.calculate_percentage_variation(df))
                                for tf, df in reshaped_data.items()}
            
            ufo_data = self.ufo_calculator.generate_ufo_data(incremental_sums)
            oscillation = self.ufo_calculator.detect_oscillations(ufo_data)
            uncertainty = self.ufo_calculator.analyze_market_uncertainty(ufo_data, oscillation)
            coherence = self.ufo_calculator.detect_timeframe_coherence(ufo_data)
            
            logging.info(f"✅ Enhanced UFO analysis completed.")
            return {
                'raw_data': ufo_data,
                'oscillation_analysis': oscillation,
                'uncertainty_metrics': uncertainty,
                'coherence_analysis': coherence
            }
        except Exception as e:
            logging.error(f"❌ UFO calculation error: {e}", exc_info=True)
            return None

    def generate_trade_decisions(self, research_result, current_positions):
        """Generates trading decisions using the TraderAgent."""
        try:
            diversification_config = {
                'min_positions_for_session': self.min_positions_for_session,
                'target_positions_when_available': self.target_positions_when_available,
                'max_concurrent_positions': self.max_concurrent_positions
            }
            decisions = self.agents['trader'].execute(
                research_result['consensus'],
                current_positions,
                diversification_config=diversification_config
            )
            logging.info("✅ Trading decisions generated.")
            return decisions
        except Exception as e:
            logging.error(f"❌ Trading decision error: {e}", exc_info=True)
            return '{"trades": []}'

    def parse_trade_decisions(self, decision_str):
        """Safely parses the JSON output from the TraderAgent."""
        try:
            json_match = re.search(r'{.*}', decision_str, re.DOTALL)
            if not json_match: return []

            json_str = json_match.group(0)
            json_str = re.sub(r'//.*?\n', '\n', json_str)
            json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
            data = json.loads(json_str)

            if 'actions' in data: return data['actions']
            if 'trade_plan' in data: return data['trade_plan']
            if 'trades' in data: return data['trades'] # Adapt to different LLM outputs
            return []
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse trade decisions JSON: {e}\nContent: {decision_str}")
            return []

    def check_session_status(self):
        """Checks if the current time is within active trading hours (8:00-20:00 GMT)."""
        return 8 <= datetime.now(pytz.timezone('GMT')).hour < 20

    def analyze_ufo_exit_signals(self, current_ufo_data, previous_ufo_data):
        """Analyzes UFO data for exit signals based on currency strength changes."""
        # This logic is critical and should be identical to the simulator's implementation
        exit_signals = []
        if not previous_ufo_data: return exit_signals
        
        current_raw = current_ufo_data.get('raw_data', {})
        previous_raw = previous_ufo_data.get('raw_data', {})

        for timeframe, current_strengths in current_raw.items():
            if timeframe not in previous_raw: continue
            
            previous_strengths = previous_raw[timeframe]
            currency_list = list(current_strengths.keys())

            for currency in currency_list:
                if currency not in previous_strengths: continue

                current_val = current_strengths[currency][-1]
                avg_previous = np.mean(previous_strengths[currency][-5:])

                change = current_val - avg_previous
                if abs(change) > 2.0: # Threshold from simulator
                    direction = "strengthening" if change > 0 else "weakening"
                    exit_signals.append({
                        'currency': currency, 'timeframe': timeframe, 'change': change,
                        'direction': direction, 'reason': f"{currency} {direction} on {timeframe}"
                    })
        return exit_signals

    def close_affected_positions(self, exit_signals):
        """Closes positions affected by strong UFO exit signals."""
        currencies_to_close = {signal['currency'] for signal in exit_signals}
        positions_closed = 0
        
        # Iterate over a copy as we may modify the list
        for pos in list(self.open_positions):
            base_curr, quote_curr = pos['symbol'][:3], pos['symbol'][3:6]
            if base_curr in currencies_to_close or quote_curr in currencies_to_close:
                logging.warning(f"🚨 Closing {pos['symbol']} due to {base_curr}/{quote_curr} exit signals.")
                if self.trade_executor.close_trade(pos['ticket']):
                    positions_closed += 1
        
        if positions_closed > 0:
            logging.info(f"📉 Closed {positions_closed} positions based on UFO exit signals.")
            self.sync_positions_from_broker() # Important to update state after closing
        return positions_closed

    def calculate_ufo_entry_price(self, symbol, direction, ufo_data):
        """Calculates an optimal entry price based on UFO strength, same as simulator."""
        try:
            # Use real-time tick data for base price
            tick = mt5.symbol_info_tick(symbol)
            if not tick: return None
            base_price = tick.ask if direction == 'BUY' else tick.bid

            if not ufo_data: return base_price

            clean_symbol = symbol.replace(self.config['mt5'].get('symbol_suffix', ''), '')
            base_currency, quote_currency = clean_symbol[:3], clean_symbol[3:6]
            
            m5_data = ufo_data.get('raw_data', {}).get(mt5.TIMEFRAME_M5, {})
            if not m5_data: return base_price

            base_strength = m5_data.get(base_currency, [0])[-1]
            quote_strength = m5_data.get(quote_currency, [0])[-1]
            strength_diff = base_strength - quote_strength

            price_adjustment = 0.0
            if abs(strength_diff) > 1.0:
                pip_size = 0.0001 if "JPY" not in symbol else 0.01
                if direction == 'BUY' and strength_diff > 0:
                    price_adjustment = -2 * pip_size # Favorable, try for better price
                elif direction == 'SELL' and strength_diff < 0:
                    price_adjustment = 2 * pip_size
            
            return base_price + price_adjustment
        except Exception as e:
            logging.error(f"Error calculating UFO entry price for {symbol}: {e}")
            return None # Let executor use market price

    def generate_cycle_summary(self):
        """Generates a summary for the completed cycle."""
        account_info = self.portfolio_manager.get_account_info()
        if not account_info: return

        unrealized_pnl = sum(p['pnl'] for p in self.open_positions)
        total_pnl = account_info.equity - self.initial_balance

        logging.info(f"📊 Cycle {self.cycle_count} Summary:")
        logging.info(f"   Trades Executed This Cycle: {len(self.trades_executed)}")
        logging.info(f"   Open Positions: {len(self.open_positions)}/{self.max_concurrent_positions}")
        logging.info(f"   Closed Trades Today: {len(self.closed_trades)}")
        logging.info(f"   Realized P&L: ${self.realized_pnl:+,.2f}")
        logging.info(f"   Unrealized P&L: ${unrealized_pnl:+,.2f}")
        logging.info(f"   Portfolio Value: ${account_info.equity:,.2f} (Total P&L: ${total_pnl:+,.2f})")

    def generate_final_summary(self):
        """Generates a final summary at the end of the trading session."""
        logging.info("\n" + "="*60 + "\n🏁 FINAL TRADING SUMMARY\n" + "="*60)
        account_info = self.portfolio_manager.get_account_info()
        if account_info:
            logging.info(f"  Ending Equity: ${account_info.equity:,.2f}")
            logging.info(f"  Ending Balance: ${account_info.balance:,.2f}")

        logging.info(f"  Total Cycles Executed: {self.cycle_count}")
        logging.info(f"  Total Trades Closed: {len(self.closed_trades)}")

        if self.closed_trades:
            total_pnl = sum(t['pnl'] for t in self.closed_trades)
            winners = sum(1 for t in self.closed_trades if t['pnl'] > 0)
            losers = len(self.closed_trades) - winners
            win_rate = (winners / len(self.closed_trades) * 100) if self.closed_trades else 0
            logging.info(f"  Total Realized P&L: ${total_pnl:,.2f}")
            logging.info(f"  Win Rate: {win_rate:.2f}% ({winners}W / {losers}L)")

    def check_and_execute_dynamic_reinforcement(self):
        """Checks for and executes dynamic reinforcement opportunities."""
        # This is a placeholder for the full implementation, which is complex.
        # For now, we ensure it's called from the monitoring loop.
        logging.info("Dynamic reinforcement check would occur here.")
        pass # To be fully implemented based on simulator's logic
