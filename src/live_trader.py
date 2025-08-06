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
from .data_collector import MT5DataCollector
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
        
        # Centralized config parsing using the new utility
        self.cycle_period_minutes = utils.get_config_value(config, 'trading', 'cycle_period_minutes', 40)
        self.cycle_period_seconds = self.cycle_period_minutes * 60
        
        self.position_update_frequency_minutes = utils.get_config_value(config, 'trading', 'position_update_frequency_minutes', 5)
        self.position_update_frequency_seconds = self.position_update_frequency_minutes * 60
        
        self.continuous_monitoring_enabled = utils.get_config_value(config, 'trading', 'continuous_monitoring_enabled', True)
        
        self.llm_client = LLMClient(api_key=config['openrouter']['api_key'])

        self.mt5_collector = MT5DataCollector(
            login=config['mt5']['login'],
            password=config['mt5']['password'],
            server=config['mt5']['server'],
            path=config['mt5']['path']
        )

        self.trade_executor = TradeExecutor(self.mt5_collector, self.config)
        self.ufo_calculator = UfoCalculator(config['trading']['currencies'].split(','))
        self.portfolio_manager = PortfolioManager(self.mt5_collector, self.trade_executor, self.config)
        self.ufo_engine = UFOTradingEngine(config, self.ufo_calculator)

        self.agents = {
            "data_analyst": DataAnalystAgent("DataAnalyst", self.mt5_collector),
            "researcher": MarketResearcherAgent("MarketResearcher", self.llm_client),
            "trader": TraderAgent("Trader", self.llm_client, self.mt5_collector),
            "risk_manager": RiskManagerAgent("RiskManager", self.llm_client, self.mt5_collector, self.config, self.portfolio_manager),
            "fund_manager": FundManagerAgent("FundManager", self.llm_client)
        }

        self.ufo_calculator = UfoCalculator(config['trading']['currencies'].split(','))

        # Initialize Dynamic Reinforcement Engine
        self.dynamic_reinforcement_engine = DynamicReinforcementEngine(config)
        if self.dynamic_reinforcement_engine.enabled:
            logging.info("✅ Dynamic Reinforcement Engine enabled")
        else:
            logging.warning("⚠️ Dynamic Reinforcement Engine disabled")

        # Portfolio tracking attributes
        self.last_portfolio_value = 0.0
        self.last_cycle_time = 0
        self.cycle_count = 0
        
        self._initialize_portfolio()

    def _setup_logging(self):
        """Configures structured logging for the application."""
        # Create file handler with UTF-8 encoding
        file_handler = logging.FileHandler("live_trader.log", encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        
        # Create console handler that avoids emoji characters
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        # Clear any existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            
        # Add our handlers
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

    def _initialize_portfolio(self):
        """Initializes portfolio by fetching account info and setting initial values."""
        if self.mt5_collector.connect():
            account_info = self.portfolio_manager.get_account_info()
            if account_info:
                self.last_portfolio_value = account_info.equity
                logging.info(f"✅ Portfolio initialized. Initial Equity: ${account_info.equity:,.2f}")
            else:
                logging.warning("⚠️ Could not retrieve account info. Using default of 0 for last portfolio value.")
                self.last_portfolio_value = 0.0
            self.mt5_collector.disconnect()
        else:
            logging.error("⚠️ MT5 connection failed during portfolio initialization.")
            self.last_portfolio_value = 0.0

    def _detect_rapid_portfolio_change(self, current_value, previous_value, threshold=0.01):
        """Detects a rapid change in portfolio value (e.g., >1% change)."""
        if previous_value == 0:
            return False, 0.0

        change_percent = (current_value - previous_value) / previous_value
        if abs(change_percent) > threshold:
            return True, change_percent
        return False, change_percent

    def continuous_position_monitoring(self, force_update=False):
        """
        High-frequency monitoring of open positions with dynamic reinforcement,
        rapid change detection, and force update capability.
        """
        now = time.time()

        # Implement force update or respect the update frequency
        if not force_update:
            if not hasattr(self, 'last_monitoring_time'):
                self.last_monitoring_time = 0

            time_since_last_update = now - self.last_monitoring_time
            if time_since_last_update < self.position_update_frequency_seconds:
                return # Not time to update yet

        self.last_monitoring_time = now

        logging.info(f"--- Continuous Position Monitoring ({datetime.now().strftime('%H:%M:%S')}) ---")
        
        # Use the new PortfolioManager to update state, which also handles position closures
        self.portfolio_manager.update_portfolio_state()

        # Rapid Change Detection
        account_info = self.portfolio_manager.get_account_info()
        if not account_info:
            logging.warning("Could not get account info for rapid change detection.")
            return

        current_portfolio_value = account_info.equity
        is_rapid_change, change_pct = self._detect_rapid_portfolio_change(
            current_portfolio_value, self.last_portfolio_value, threshold=0.01
        )
        if is_rapid_change:
            logging.warning(f"🚨 RAPID PORTFOLIO CHANGE DETECTED: {change_pct:+.2%}")
            # Trigger immediate reinforcement check due to volatility
            self.check_and_execute_dynamic_reinforcement(triggered_by_volatility=True)

        # Update the last known portfolio value for the next check
        self.last_portfolio_value = current_portfolio_value

        # Check for dynamic reinforcement opportunities
        if self.dynamic_reinforcement_engine.enabled:
            self.check_and_execute_dynamic_reinforcement()
        
        # Logging the summary using the new portfolio manager's data
        unrealized_pnl = self.portfolio_manager.unrealized_pnl
        open_positions_count = len(self.portfolio_manager.open_positions)
        logging.info(f"💰 Portfolio Value: ${current_portfolio_value:,.2f} | Open Positions: {open_positions_count} | Unrealized P&L: ${unrealized_pnl:,.2f}")
        logging.info(f"{self.portfolio_manager.get_portfolio_summary()}")
        logging.info("--- End of Monitoring ---")
    

    def run_main_trading_cycle(self):
        """
        Runs the main agentic workflow for making new trading decisions.
        """
        logging.info("\n" + "="*60)
        logging.info(f"🚀 Starting New Trading Cycle at {datetime.now().strftime('%H:%M:%S')}")
        logging.info("="*60)

        # 2. Data Collection for all symbols
        symbols = self.config['trading']['symbols'].split(',')
        symbol_suffix = self.config['mt5'].get('symbol_suffix', '')
        timeframes = [mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15, mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, mt5.TIMEFRAME_D1]
        timeframe_bars = {
            mt5.TIMEFRAME_M5: 240,
            mt5.TIMEFRAME_M15: 80,
            mt5.TIMEFRAME_H1: 20,
            mt5.TIMEFRAME_H4: 120,
            mt5.TIMEFRAME_D1: 100
        }

        all_price_data = {}
        for symbol in symbols:
            symbol_with_suffix = symbol + symbol_suffix
            data = self.agents['data_analyst'].execute({
                'source': 'mt5',
                'symbol': symbol_with_suffix,
                'timeframes': timeframes,
                'num_bars': timeframe_bars
            })
            if data:
                all_price_data[symbol] = data

        if not all_price_data:
            logging.warning("Could not fetch price data for any symbol. Retrying in 60 seconds...")
            time.sleep(60)
            return

        # 3. UFO Calculation - with robust data validation
        reshaped_data = {}
        valid_data_count = 0
        
        for symbol, timeframe_data in all_price_data.items():
            if timeframe_data is None:
                logging.warning(f"No timeframe data for symbol {symbol}")
                continue
                
            for timeframe, df in timeframe_data.items():
                if df is None or df.empty:
                    logging.warning(f"No data for {symbol} on timeframe {timeframe}")
                    continue
                    
                if 'close' not in df.columns:
                    logging.error(f"Missing 'close' column for {symbol} on timeframe {timeframe}")
                    continue
                    
                try:
                    if timeframe not in reshaped_data:
                        reshaped_data[timeframe] = pd.DataFrame()
                    reshaped_data[timeframe][symbol] = df['close']
                    valid_data_count += 1
                except Exception as e:
                    logging.error(f"Error processing data for {symbol} on timeframe {timeframe}: {e}")
                    continue
        
        if valid_data_count == 0:
            logging.error("No valid market data available for UFO calculation. Skipping this cycle.")
            return

        incremental_sums_dict = {}
        for timeframe, price_df in reshaped_data.items():
            variation_data = self.ufo_calculator.calculate_percentage_variation(price_df)
            incremental_sums_dict[timeframe] = self.ufo_calculator.calculate_incremental_sum(variation_data)

        ufo_data = self.ufo_calculator.generate_ufo_data(incremental_sums_dict)

        oscillation_analysis = self.ufo_calculator.detect_oscillations(ufo_data)
        uncertainty_metrics = self.ufo_calculator.analyze_market_uncertainty(ufo_data, oscillation_analysis)
        coherence_analysis = self.ufo_calculator.detect_timeframe_coherence(ufo_data)

        enhanced_ufo_data = {
            'raw_data': ufo_data,
            'oscillation_analysis': oscillation_analysis,
            'uncertainty_metrics': uncertainty_metrics,
            'coherence_analysis': coherence_analysis
        }

        # Store UFO data for reinforcement analysis
        self.last_ufo_data = enhanced_ufo_data
        
        # 4. First Priority: UFO Portfolio Management
        open_positions_df = self.agents['risk_manager'].portfolio_manager.get_positions()
        if open_positions_df is not None and not open_positions_df.empty:
            logging.info(f"\n--- UFO Portfolio Management: {len(open_positions_df)} positions ---")
            
            # Analyze all positions for reinforcement opportunities
            self.analyze_positions_for_reinforcement()

            account_info = self.mt5_collector.connect() and mt5.account_info()
            if account_info:
                portfolio_stop_breached, stop_reason = self.ufo_engine.check_portfolio_equity_stop(
                    account_info.balance, account_info.equity
                )
                if portfolio_stop_breached:
                    logging.critical(f"🚨 UFO PORTFOLIO STOP TRIGGERED: {stop_reason}")
                    for _, position in open_positions_df.iterrows():
                        self.trade_executor.close_trade(position.ticket)
                    logging.critical("🚨 All positions closed. Waiting 5 minutes before resuming...")
                    time.sleep(300)
                    return

            economic_events_for_session = self.agents['data_analyst'].execute({'source': 'economic_calendar'})
            should_close, close_reason = self.ufo_engine.should_close_for_session_end(economic_events_for_session)
            if should_close:
                logging.info(f"🌅 UFO SESSION END: {close_reason}")
                for _, position in open_positions_df.iterrows():
                    self.trade_executor.close_trade(position.ticket)
                time.sleep(300)
                return

            current_market_data = self.get_real_time_market_data_for_positions(open_positions_df)
            for _, position in open_positions_df.iterrows():
                should_reinforce, reason, reinforcement_plan = self.ufo_engine.should_reinforce_position(
                    position, enhanced_ufo_data, current_market_data
                )
                
                if should_reinforce:
                    logging.info(f"🔧 UFO Compensation: {reason}")
                    success, result_msg = self.ufo_engine.execute_compensation_trade(
                        position, reinforcement_plan, self.trade_executor
                    )
                    if success:
                        logging.info(f"✅ {result_msg}")
                    else:
                        logging.error(f"❌ Compensation failed: {result_msg}")
                elif "close position" in reason:
                    logging.info(f"📊 UFO Analysis: Closing {position.ticket} - {reason}")
                    self.trade_executor.close_trade(position.ticket)
                else:
                    logging.info(f"📈 Position {position.ticket} - {reason}")

        # 5. Agentic Workflow for new trade decisions
        economic_events = self.agents['data_analyst'].execute({'source': 'economic_calendar'})
        open_positions_df = self.agents['risk_manager'].portfolio_manager.get_positions()
        research_result = self.agents['researcher'].execute(enhanced_ufo_data, economic_events)

        diversification_config = {
            'min_positions_for_session': self.ufo_engine.min_positions_for_session,
            'target_positions_when_available': self.ufo_engine.target_positions_when_available,
            'max_concurrent_positions': self.ufo_engine.max_concurrent_positions
        }

        trade_decision_str = self.agents['trader'].execute(
            research_result['consensus'],
            open_positions_df,
            diversification_config=diversification_config
        )

        risk_assessment = self.agents['risk_manager'].execute(trade_decision_str)

        if risk_assessment['portfolio_risk_status'] == "STOP_LOSS_BREACHED":
            logging.critical("!!! EQUITY STOP LOSS BREACHED. CEASING ALL TRADING. !!!")
            # This should be handled more gracefully, maybe break the loop
            return

        authorization = self.agents['fund_manager'].execute(trade_decision_str, risk_assessment)

        # 6. Output with Diversification Status
        position_count = len(open_positions_df) if open_positions_df is not None else 0
        diversification_status = f"📊 Portfolio Diversification: {position_count}/{self.ufo_engine.max_concurrent_positions} positions"

        if position_count < self.ufo_engine.min_positions_for_session:
            diversification_status += " ⚠️ Below minimum"
        elif position_count >= self.ufo_engine.target_positions_when_available:
            diversification_status += " ✅ Well diversified"
        else:
            diversification_status += " 📈 Building diversification"

        logging.info("\n--- Live Trading Cycle Summary ---")
        logging.info(f"Timestamp: {pd.Timestamp.now()}")
        logging.info(diversification_status)
        logging.info(f"Research Consensus: {research_result['consensus']}")
        logging.info(f"Trade Decision: {trade_decision_str}")
        logging.info(f"Risk Assessment: {risk_assessment}")
        logging.info(f"Final Authorization: {authorization}")

        # ... (trade execution logic) ...

        # 8. Generate Cycle Summary
        self.generate_cycle_summary()

    def generate_cycle_summary(self):
        """
        Generates and logs a summary of the completed trading cycle.
        """
        logging.info("\n" + "---" * 20)
        logging.info(f"✅ CYCLE {self.cycle_count} SUMMARY at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info("---" * 20)

        # Portfolio Status from the single source of truth
        account_info = self.portfolio_manager.get_account_info()
        if account_info:
            logging.info(f"  Portfolio Equity: ${account_info.equity:,.2f} | Unrealized P&L: ${self.portfolio_manager.unrealized_pnl:,.2f}")

        # Open Positions
        open_positions = self.portfolio_manager.open_positions
        logging.info(f"  Open Positions: {len(open_positions)}")
        for ticket, pos in open_positions.items():
            # To get live P&L, we need to fetch the position again or trust the last update
            # For summary, we'll just show what we have stored.
            logging.info(f"    - #{ticket} {pos['symbol']} {pos['direction']} {pos['volume']} lots")

        # UFO Analysis Summary
        if hasattr(self, 'last_ufo_data') and self.last_ufo_data:
            uncertainty = self.last_ufo_data.get('uncertainty_metrics', {}).get(mt5.TIMEFRAME_M5, {})
            if uncertainty:
                logging.info(f"  Market State (M5): {uncertainty.get('overall_state', 'N/A')} (Confidence: {uncertainty.get('confidence_level', 'N/A')})")

        logging.info("---" * 20 + "\n")

    def generate_final_summary(self):
        """Generates and logs a final summary at the end of the trading session."""
        logging.info("\n" + "="*60)
        logging.info("🏁 FINAL TRADING SUMMARY")
        logging.info("="*60)

        account_info = self.portfolio_manager.get_account_info()
        if account_info:
            logging.info(f"  Ending Equity: ${account_info.equity:,.2f}")
            logging.info(f"  Ending Balance: ${account_info.balance:,.2f}")

        logging.info(f"  Total Cycles Executed: {self.cycle_count}")

        closed_trades = self.portfolio_manager.get_trade_history()

        logging.info(f"  Total Trades Closed: {len(closed_trades)}")
        if closed_trades:
            total_pnl = sum(trade['pnl'] for trade in closed_trades)
            winners = sum(1 for trade in closed_trades if trade['pnl'] > 0)
            losers = len(closed_trades) - winners
            win_rate = (winners / len(closed_trades)) * 100 if closed_trades else 0
            logging.info(f"  Total Realized P&L: ${total_pnl:,.2f}")
            logging.info(f"  Win Rate: {win_rate:.2f}% ({winners}W / {losers}L)")

        self.save_full_day_report(account_info, closed_trades)
        logging.info("="*60)

    def save_full_day_report(self, account_info, closed_trades):
        """Saves a detailed report of the trading day to a file."""
        filename = f"full_day_report_{datetime.now().strftime('%Y%m%d')}.txt"

        report = []
        report.append("="*40)
        report.append(f"UFO TRADING REPORT - {datetime.now().strftime('%Y-%m-%d')}")
        report.append("="*40 + "\n")

        if account_info:
            report.append(f"Ending Equity: ${account_info.equity:,.2f}")
            report.append(f"Ending Balance: ${account_info.balance:,.2f}\n")

        report.append(f"Total Cycles: {self.cycle_count}")
        report.append(f"Total Closed Trades: {len(closed_trades)}\n")

        if closed_trades:
            report.append("-" * 40)
            report.append("CLOSED TRADES:")
            report.append("-" * 40)
            for trade in closed_trades:
                report.append(
                    f"  - Ticket: {trade['ticket']}, Symbol: {trade['symbol']}, "
                    f"P&L: ${trade['pnl']:.2f}, Comment: {trade.get('comment', 'N/A')}"
                )
            report.append("-" * 40 + "\n")

        report_str = "\n".join(report)

        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(report_str)
            logging.info(f"Full report saved to '{filename}'")
        except Exception as e:
            logging.error(f"Failed to save full day report: {e}")

        return report_str

    def run(self):
        """
        Runs the main trading loop, orchestrating the main cycle and continuous monitoring.
        Similar to simulation's run_full_day_simulation() but continuous.
        """
        self.last_cycle_time = time.time() - self.cycle_period_seconds - 1 # Ensure the first cycle runs immediately
        
        logging.info(f"🚀 Starting Live Trading")
        logging.info(f"⏰ Cycle Frequency: Every {self.cycle_period_minutes} minutes")
        if self.continuous_monitoring_enabled:
            logging.info(f"📊 Continuous Monitoring: Position updates every {self.position_update_frequency_minutes} minutes")

        try:
            while True:
                try:
                    now = time.time()
                    
                    # Continuous position monitoring between cycles (like simulation)
                    if self.continuous_monitoring_enabled and self.open_positions:
                        # Check if it's time for position monitoring
                        time_since_last_cycle = now - self.last_cycle_time
                        monitoring_intervals = int(time_since_last_cycle / self.position_update_frequency_seconds)
                        if monitoring_intervals > 0:
                            self.continuous_position_monitoring()
                    
                    # Check if it's time for the main trading cycle
                    if now - self.last_cycle_time >= self.cycle_period_seconds:
                        self.cycle_count += 1  # Increment cycle counter
                        
                        if self.ufo_engine.is_active_session():
                            self.run_main_trading_cycle()
                        else:
                            logging.info(f"({datetime.now().strftime('%H:%M:%S')}) Outside active trading session. Skipping main cycle.")
                        self.last_cycle_time = now
                    
                    # Calculate time to next cycle
                    time_to_next_cycle = (self.last_cycle_time + self.cycle_period_seconds) - now
                    
                    # Sleep until next monitoring interval or cycle
                    if self.continuous_monitoring_enabled and self.open_positions:
                        sleep_duration = min(self.position_update_frequency_seconds, max(1, time_to_next_cycle))
                    else:
                        sleep_duration = min(60, max(1, time_to_next_cycle))
                    
                    if time_to_next_cycle > 60:
                        logging.info(f"--- Next cycle in {time_to_next_cycle:.0f} seconds ---")
                    time.sleep(sleep_duration)

                except KeyboardInterrupt:
                    logging.info("\nTrading interrupted by user. Exiting...")
                    break
                except Exception as e:
                    logging.critical(f"Error in main trading loop: {e}")
                    import traceback
                    traceback.print_exc()
                    logging.info("Waiting 60 seconds before retrying...")
                    time.sleep(60)
        finally:
            # Generate and save the final report
            self.generate_final_summary()
    
    def check_and_execute_dynamic_reinforcement(self):
        """
        Enhanced dynamic reinforcement checking and execution.
        Similar to simulation's simulate_realistic_position_tracking().
        """
        if not self.open_positions:
            return
        
        try:
            # Get current market data for all open positions
            positions_df = self.agents['risk_manager'].portfolio_manager.get_positions()
            if positions_df is None or positions_df.empty:
                return
            
            current_market_data = self.get_real_time_market_data_for_positions(positions_df)
            
            # Get current UFO data for analysis
            current_ufo_data = getattr(self, 'last_ufo_data', None)
            
            # Check each position for reinforcement opportunities
            positions_requiring_reinforcement = []
            
            for _, position in positions_df.iterrows():
                # Detect market events that might trigger reinforcement
                market_events = self.dynamic_reinforcement_engine.detect_market_events(
                    [position], current_market_data, current_ufo_data
                )
                
                if market_events:
                    for event in market_events:
                        # Calculate dynamic reinforcement plan
                        reinforcement_plan = self.dynamic_reinforcement_engine.calculate_dynamic_reinforcement(
                            position, event, current_market_data, current_ufo_data
                        )
                        
                        if reinforcement_plan and reinforcement_plan.get('execute', False):
                            positions_requiring_reinforcement.append((position, reinforcement_plan, event))
            
            # Execute reinforcement trades
            for position, plan, event in positions_requiring_reinforcement:
                self.execute_dynamic_reinforcement(position, plan, event)
                
        except Exception as e:
            logging.error(f"❌ Error in dynamic reinforcement check: {e}")
            import traceback
            logging.error(traceback.format_exc())
    
    def execute_dynamic_reinforcement(self, position, reinforcement_plan, market_event):
        """
        Execute a dynamic reinforcement trade based on the calculated plan.
        Enhanced version with UFO-optimized entry prices.
        """
        try:
            compensation_type = reinforcement_plan.get('type', 'dynamic')
            additional_lots = reinforcement_plan.get('additional_lots', 0.0)
            reason = reinforcement_plan.get('reason', 'Market event trigger')
            
            if additional_lots <= 0:
                logging.warning(f"⚠️ Invalid reinforcement lots: {additional_lots}")
                return False
            
            # Check if we've already reinforced this position recently
            reinforcement_status = self.dynamic_reinforcement_engine.get_reinforcement_status(position)
            if reinforcement_status.get('can_reinforce', False) is False:
                logging.info(f"⏳ Position {position.ticket} in cooling period: {reinforcement_status.get('reason')}")
                return False
            
            logging.info(f"🔧 Dynamic Reinforcement Triggered: {compensation_type.upper()}")
            logging.info(f"   Position: {position.symbol} ({position.ticket})")
            logging.info(f"   Event: {market_event.get('type', 'unknown')}")
            logging.info(f"   Reason: {reason}")
            logging.info(f"   Additional lots: {additional_lots:.2f}")
            
            # Calculate optimal entry price using UFO methodology
            optimal_entry_price = self.calculate_ufo_optimized_entry_price(
                position.symbol,
                'BUY' if position.type == 0 else 'SELL',
                reinforcement_plan,
                market_event
            )
            
            # Prepare the reinforcement trade
            trade_direction = 'BUY' if position.type == 0 else 'SELL'
            trade_type = mt5.ORDER_TYPE_BUY if position.type == 0 else mt5.ORDER_TYPE_SELL
            
            # Add comment with details
            comment = f"UFO {compensation_type} for #{position.ticket}"
            
            # Execute the reinforcement trade
            result = self.trade_executor.execute_ufo_trade(
                symbol=position.symbol,
                trade_type=trade_type,
                volume=additional_lots,
                comment=comment
            )
            
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logging.info(f"✅ Reinforcement executed successfully!")
                logging.info(f"   New ticket: {result.order}")
                logging.info(f"   Executed at: {result.price if hasattr(result, 'price') else 'market price'}")
                
                # Record the reinforcement
                self.dynamic_reinforcement_engine.record_reinforcement(position, reinforcement_plan)
                
                # Track the reinforcement in our position list
                new_position = {
                    'ticket': result.order,
                    'symbol': position.symbol,
                    'direction': trade_direction,
                    'volume': additional_lots,
                    'entry_price': result.price if hasattr(result, 'price') else optimal_entry_price,
                    'current_price': result.price if hasattr(result, 'price') else optimal_entry_price,
                    'pnl': 0.0,
                    'timestamp': datetime.now(),
                    'peak_pnl': 0.0,
                    'original_position_ticket': position.ticket,
                    'reinforcement_type': compensation_type,
                    'reinforcement_reason': reason
                }
                self.open_positions.append(new_position)
                
                return True
            else:
                error_msg = f"Reinforcement trade failed"
                if result:
                    error_msg += f" - RetCode: {result.retcode}"
                    if hasattr(result, 'comment'):
                        error_msg += f" - {result.comment}"
                logging.error(f"❌ {error_msg}")
                return False
                
        except Exception as e:
            logging.error(f"❌ Error executing dynamic reinforcement: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False
    
    def calculate_ufo_optimized_entry_price(self, symbol, direction, reinforcement_plan, market_event):
        """
        Calculate the optimal entry price for a reinforcement trade using UFO methodology.
        This considers market conditions, UFO signals, and the type of reinforcement.
        """
        try:
            # Get current market data
            current_market_data = self.get_real_time_market_data_for_positions([{'symbol': symbol}])
            
            if symbol not in current_market_data:
                logging.warning(f"⚠️ No market data for {symbol}, using market execution")
                return None
            
            current_data = current_market_data[symbol]
            bid = current_data['bid']
            ask = current_data['ask']
            spread = current_data['spread']
            
            # Base price depends on direction
            if direction == 'BUY':
                base_price = ask
            else:
                base_price = bid
            
            # Adjust based on reinforcement type and market event
            reinforcement_type = reinforcement_plan.get('type', 'standard')
            event_type = market_event.get('type', '')
            
            price_adjustment = 0.0
            
            # Apply adjustments based on reinforcement strategy
            if 'momentum' in reinforcement_type.lower():
                # For momentum reinforcement, enter slightly ahead of current price
                if direction == 'BUY':
                    price_adjustment = spread * 0.5  # Pay half spread extra for urgency
                else:
                    price_adjustment = -spread * 0.5
                    
            elif 'compensation' in reinforcement_type.lower():
                # For compensation, try to get better price
                if direction == 'BUY':
                    price_adjustment = -spread * 0.25  # Try to buy slightly lower
                else:
                    price_adjustment = spread * 0.25  # Try to sell slightly higher
                    
            elif 'rapid_loss' in event_type.lower():
                # For rapid loss recovery, execute immediately at market
                price_adjustment = 0.0
                
            elif 'volatility' in event_type.lower():
                # In high volatility, add buffer for slippage
                volatility_multiplier = market_event.get('volatility_multiplier', 1.0)
                if direction == 'BUY':
                    price_adjustment = spread * volatility_multiplier
                else:
                    price_adjustment = -spread * volatility_multiplier
            
            # Calculate final optimal price
            optimal_price = base_price + price_adjustment
            
            # Apply sanity checks
            if direction == 'BUY':
                # Don't pay more than 2 spreads above ask
                max_price = ask + (spread * 2)
                optimal_price = min(optimal_price, max_price)
            else:
                # Don't sell for less than 2 spreads below bid
                min_price = bid - (spread * 2)
                optimal_price = max(optimal_price, min_price)
            
            logging.info(f"💹 Optimal entry price calculated: {optimal_price:.5f}")
            logging.info(f"   Base: {base_price:.5f}, Adjustment: {price_adjustment:.5f}")
            
            return optimal_price
            
        except Exception as e:
            logging.error(f"❌ Error calculating optimal entry price: {e}")
            return None
    
    def calculate_ufo_entry_price(self, symbol, direction, ufo_data=None, use_strength=True):
        """
        Calculate optimal entry price based on UFO methodology, integrating historical
        price data and refined currency strength analysis.
        """
        try:
            # --- 1. Get Market and Historical Data ---
            current_market_data = self.get_real_time_market_data_for_positions([{'symbol': symbol}])
            if symbol not in current_market_data:
                logging.warning(f"⚠️ No market data for {symbol}, cannot calculate entry price.")
                return None
            
            current_data = current_market_data[symbol]
            bid, ask, spread = current_data['bid'], current_data['ask'], current_data['spread']
            
            # Get 15 minutes of M1 data for momentum/volatility analysis
            hist_data = self.mt5_collector.get_historical_data(symbol, mt5.TIMEFRAME_M1, num_bars=15)

            # --- 2. Analyze Short-Term Momentum & Volatility ---
            momentum = 0
            if hist_data is not None and not hist_data.empty:
                price_changes = hist_data['close'].diff().dropna()
                if not price_changes.empty:
                    momentum = price_changes.mean()

            # --- 3. Determine Base Price and Initial Adjustment ---
            base_price = ask if direction == 'BUY' else bid
            price_adjustment = 0.0

            # --- 4. Refined UFO Strength Analysis ---
            if ufo_data and use_strength:
                clean_symbol = symbol.replace('-ECN', '').replace('/', '')
                if len(clean_symbol) >= 6:
                    base_currency, quote_currency = clean_symbol[:3], clean_symbol[3:6]
                    base_strength = self._get_currency_strength_from_ufo(base_currency, ufo_data)
                    quote_strength = self._get_currency_strength_from_ufo(quote_currency, ufo_data)
                    strength_diff = base_strength - quote_strength

                    logging.info(f"🔬 UFO Strength Analysis for {symbol}: {base_currency}({base_strength:.2f}) vs {quote_currency}({quote_strength:.2f}) -> Diff: {strength_diff:.2f}")

                    # Refined adjustment: scale adjustment based on strength magnitude
                    # A larger diff gives a larger credit for waiting for a better price.
                    strength_adjustment_factor = np.clip(strength_diff / 10.0, -1, 1) # Normalize to [-1, 1]
                    
                    if direction == 'BUY':
                        # Favorable (strong base): negative adjustment (lower price)
                        # Unfavorable (weak base): positive adjustment (higher price)
                        price_adjustment = -strength_adjustment_factor * spread * 0.5 # Max adjustment is 50% of spread
                    else: # SELL
                        # Favorable (weak base): positive adjustment (higher price)
                        # Unfavorable (strong base): negative adjustment (lower price)
                        price_adjustment = strength_adjustment_factor * spread * 0.5

                    logging.info(f"   Strength adjustment: {price_adjustment:.5f}")

            # --- 5. Integrate Momentum ---
            momentum_adjustment = 0
            if (direction == 'BUY' and momentum > 0) or (direction == 'SELL' and momentum < 0):
                # Momentum agrees with the trade, be less patient
                momentum_adjustment = (spread * 0.15) if direction == 'BUY' else (-spread * 0.15)
                logging.info(f"   Momentum agrees. Adjustment: {momentum_adjustment:.5f}")
            
            price_adjustment += momentum_adjustment

            # --- 6. Other UFO Factor Adjustments (Uncertainty, Oscillation) ---
            if ufo_data:
                # Uncertainty: be more conservative (accept worse price)
                uncertainty_level = ufo_data.get('uncertainty_metrics', {}).get('M5', {}).get('uncertainty_ratio', 0.0)
                if uncertainty_level > 0.6:
                    uncertainty_adj = (spread * 0.1) if direction == 'BUY' else (-spread * 0.1)
                    price_adjustment += uncertainty_adj
                    logging.info(f"   High uncertainty. Adjustment: {uncertainty_adj:.5f}")
            
            # --- 7. Calculate Final Price with Sanity Checks ---
            optimal_price = base_price + price_adjustment

            # Sanity checks to keep the price reasonable
            if direction == 'BUY':
                max_price = ask + (spread * 1.2) # Don't pay >120% of spread
                min_price = ask - (spread * 0.8) # Don't expect >80% spread improvement
                optimal_price = np.clip(optimal_price, min_price, max_price)
            else: # SELL
                min_price = bid - (spread * 1.2)
                max_price = bid + (spread * 0.8)
                optimal_price = np.clip(optimal_price, min_price, max_price)
            
            logging.info(f"💰 UFO Optimal Entry Price for {symbol}: {optimal_price:.5f}")
            logging.info(f"   Market: Ask={ask:.5f}, Bid={bid:.5f}")
            logging.info(f"   Total Adjustment: {price_adjustment:.5f} ({((optimal_price/base_price)-1)*100:.4f}%)")

            return optimal_price

        except Exception as e:
            logging.error(f"❌ Error calculating UFO entry price: {e}")
            import traceback
            logging.error(traceback.format_exc())
            # Fallback to market price
            return ask if direction == 'BUY' else bid
    
    def _get_currency_strength_from_ufo(self, currency, ufo_data, timeframe=None):
        """
        Extract currency strength from UFO data.
        
        Args:
            currency: Currency code (e.g., 'EUR', 'USD')
            ufo_data: Enhanced UFO data structure
            timeframe: Specific timeframe to use (default: M5)
        
        Returns:
            Currency strength value or 0.0 if not found
        """
        try:
            # Use M5 as default primary timeframe
            if timeframe is None:
                timeframe = mt5.TIMEFRAME_M5
            
            # Extract raw UFO data
            raw_data = ufo_data.get('raw_data', ufo_data)
            
            if timeframe not in raw_data:
                logging.warning(f"Timeframe {timeframe} not found in UFO data")
                return 0.0
            
            strength_data = raw_data[timeframe]
            
            # Handle both DataFrame and dict formats
            if hasattr(strength_data, 'columns'):
                # DataFrame format
                if currency in strength_data.columns:
                    # Get the latest value
                    return float(strength_data[currency].iloc[-1])
            elif isinstance(strength_data, dict):
                # Dict format
                if currency in strength_data:
                    values = strength_data[currency]
                    if isinstance(values, list):
                        return float(values[-1]) if values else 0.0
                    else:
                        return float(values)
            
            logging.warning(f"Currency {currency} not found in UFO data")
            return 0.0
            
        except Exception as e:
            logging.error(f"Error extracting currency strength for {currency}: {e}")
            return 0.0
    
    def analyze_positions_for_reinforcement(self):
        """
        Comprehensive position analysis for reinforcement opportunities.
        This is called during the main trading cycle.
        """
        if not self.dynamic_reinforcement_engine.enabled:
            return
        
        try:
            positions_df = self.agents['risk_manager'].portfolio_manager.get_positions()
            if positions_df is None or positions_df.empty:
                return
            
            logging.info("🔍 Analyzing positions for reinforcement opportunities...")
            
            # Get comprehensive market data
            current_market_data = self.get_real_time_market_data_for_positions(positions_df)
            current_ufo_data = getattr(self, 'last_ufo_data', None)
            
            reinforcement_opportunities = []
            
            for _, position in positions_df.iterrows():
                # Check UFO-based reinforcement signals
                if current_ufo_data:
                    should_reinforce, reason, reinforcement_plan = self.ufo_engine.should_reinforce_position(
                        position, current_ufo_data, current_market_data
                    )
                    
                    if should_reinforce and reinforcement_plan:
                        reinforcement_opportunities.append({
                            'position': position,
                            'plan': reinforcement_plan,
                            'reason': reason,
                            'type': 'UFO-based'
                        })
                
                # Check dynamic reinforcement signals
                market_events = self.dynamic_reinforcement_engine.detect_market_events(
                    [position], current_market_data, current_ufo_data
                )
                
                for event in market_events:
                    plan = self.dynamic_reinforcement_engine.calculate_dynamic_reinforcement(
                        position, event, current_market_data, current_ufo_data
                    )
                    
                    if plan and plan.get('execute', False):
                        reinforcement_opportunities.append({
                            'position': position,
                            'plan': plan,
                            'reason': event.get('description', 'Market event'),
                            'type': 'Dynamic'
                        })
            
            # Execute the most critical reinforcements
            if reinforcement_opportunities:
                logging.info(f"📊 Found {len(reinforcement_opportunities)} reinforcement opportunities")
                
                # Sort by priority (if specified in plan)
                reinforcement_opportunities.sort(
                    key=lambda x: x['plan'].get('priority', 0),
                    reverse=True
                )
                
                # Execute top opportunities (limit to prevent over-leveraging)
                max_reinforcements_per_cycle = 3
                executed_count = 0
                
                for opportunity in reinforcement_opportunities[:max_reinforcements_per_cycle]:
                    logging.info(f"\n🎯 Executing {opportunity['type']} reinforcement:")
                    logging.info(f"   Position: {opportunity['position'].symbol} #{opportunity['position'].ticket}")
                    logging.info(f"   Reason: {opportunity['reason']}")
                    
                    if opportunity['type'] == 'UFO-based':
                        success, result_msg = self.ufo_engine.execute_compensation_trade(
                            opportunity['position'],
                            opportunity['plan'],
                            self.trade_executor
                        )
                        if success:
                            logging.info(f"✅ {result_msg}")
                            executed_count += 1
                        else:
                            logging.error(f"❌ {result_msg}")
                    else:
                        # Dynamic reinforcement
                        event = {'type': opportunity['type'], 'description': opportunity['reason']}
                        if self.execute_dynamic_reinforcement(
                            opportunity['position'],
                            opportunity['plan'],
                            event
                        ):
                            executed_count += 1
                
                logging.info(f"\n📈 Reinforcement summary: {executed_count}/{len(reinforcement_opportunities)} executed")
            else:
                logging.info("✔️ No reinforcement opportunities at this time")
                
        except Exception as e:
            logging.error(f"❌ Error in reinforcement analysis: {e}")
            import traceback
            logging.error(traceback.format_exc())

    def get_real_time_market_data_for_positions(self, open_positions, use_cache=True):
        """
        Collect real-time market data for all open positions with caching support.
        Enhanced version with better error handling and support for both DataFrame and list formats.
        
        Args:
            open_positions: DataFrame or list of open positions
            use_cache: Whether to use cached data to prevent excessive API calls
        """
        current_market_data = {}
        
        # Handle empty positions
        if open_positions is None:
            return current_market_data
            
        # Check if positions is empty (works for both DataFrame and list)
        if hasattr(open_positions, 'empty'):
            if open_positions.empty:
                return current_market_data
        elif hasattr(open_positions, '__len__'):
            if len(open_positions) == 0:
                return current_market_data
        else:
            return current_market_data
        
        # Cache management for high-frequency calls
        current_time = pd.Timestamp.now()
        cache_key = f"market_data_{current_time.floor('1S')}"  # Cache per second
        
        if use_cache:
            # Initialize cache if needed
            if not hasattr(self, '_market_data_cache'):
                self._market_data_cache = {}
                self._cache_timestamps = {}
            
            # Return cached data if fresh (less than 1 second old)
            if cache_key in self._market_data_cache:
                cache_age = (current_time - self._cache_timestamps[cache_key]).total_seconds()
                if cache_age < 1.0:  # Use cached data if less than 1 second old
                    return self._market_data_cache[cache_key]
            
        try:
            if not self.mt5_collector.connect():
                logging.warning("⚠️ Failed to connect to MT5 for market data collection")
                return current_market_data
            
            # Extract unique symbols from positions (handle both DataFrame and list)
            symbols_to_fetch = set()
            
            if hasattr(open_positions, 'iterrows'):
                # DataFrame format
                for _, position in open_positions.iterrows():
                    symbols_to_fetch.add(position['symbol'])
            elif hasattr(open_positions, '__iter__'):
                # List/iterable format
                for position in open_positions:
                    if isinstance(position, dict):
                        symbols_to_fetch.add(position['symbol'])
                    elif hasattr(position, 'symbol'):
                        symbols_to_fetch.add(position.symbol)
            else:
                # Single position
                if hasattr(open_positions, 'symbol'):
                    symbols_to_fetch.add(open_positions.symbol)
            
            # Fetch market data for each symbol
            successful_fetches = 0
            for symbol in symbols_to_fetch:
                try:
                    # Try to get tick data first (most accurate)
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is not None and tick.bid > 0:
                        current_market_data[symbol] = {
                            'close': tick.bid,
                            'ask': tick.ask,
                            'bid': tick.bid,
                            'spread': tick.ask - tick.bid,
                            'last': tick.last if hasattr(tick, 'last') else tick.bid,
                            'volume': tick.volume if hasattr(tick, 'volume') else 0,
                            'timestamp': current_time
                        }
                        successful_fetches += 1
                    else:
                        # Fallback to recent bar data if tick is not available
                        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
                        if rates is not None and len(rates) > 0:
                            close_price = rates[0]['close']
                            # Estimate spread based on symbol type
                            if 'JPY' in symbol:
                                estimated_spread = 0.01  # 1 pip for JPY pairs
                            else:
                                estimated_spread = 0.0001  # 1 pip for other pairs
                            
                            current_market_data[symbol] = {
                                'close': close_price,
                                'ask': close_price + estimated_spread,
                                'bid': close_price,
                                'spread': estimated_spread,
                                'last': close_price,
                                'volume': rates[0]['tick_volume'] if 'tick_volume' in rates[0] else 0,
                                'timestamp': current_time
                            }
                            successful_fetches += 1
                        else:
                            logging.warning(f"⚠️ No market data available for {symbol}")
                    
                except Exception as e:
                    logging.error(f"❌ Error getting market data for {symbol}: {e}")
                    # Try to use last known good data if available
                    if hasattr(self, '_last_known_prices') and symbol in self._last_known_prices:
                        current_market_data[symbol] = self._last_known_prices[symbol]
                        logging.info(f"📊 Using last known price for {symbol}")
                    continue
            
            # Store successful fetches as last known prices
            if not hasattr(self, '_last_known_prices'):
                self._last_known_prices = {}
            self._last_known_prices.update(current_market_data)
            
            # Log summary
            if successful_fetches > 0:
                logging.info(f"📊 Market data collected for {successful_fetches}/{len(symbols_to_fetch)} symbols")
            
            # Update cache
            if use_cache and current_market_data:
                self._market_data_cache[cache_key] = current_market_data
                self._cache_timestamps[cache_key] = current_time
                
                # Clean old cache entries (keep only last 60 seconds of data)
                cutoff_time = current_time - pd.Timedelta(seconds=60)
                keys_to_remove = [k for k, t in self._cache_timestamps.items() if t < cutoff_time]
                for key in keys_to_remove:
                    del self._market_data_cache[key]
                    del self._cache_timestamps[key]
            
            self.mt5_collector.disconnect()
            
        except Exception as e:
            logging.error(f"❌ Critical error in market data collection: {e}")
            import traceback
            logging.error(traceback.format_exc())
            
        return current_market_data
    
    def check_portfolio_status(self):
        """
        Checks overall portfolio status using UFO methodology.
        """
        try:
            positions = self.agents['risk_manager'].portfolio_manager.get_positions()
            if positions is None or len(positions) == 0:
                return
                
            portfolio_value = self.ufo_engine.calculate_portfolio_synthetic_value()
            logging.info(f"Portfolio synthetic value: {portfolio_value:.2f}%")
            
            if portfolio_value <= -5.0:  # Portfolio stop loss threshold
                logging.critical("Portfolio stop loss triggered - closing all positions")
                for _, position in positions.iterrows():
                    self.trade_executor.close_trade(position.ticket)
                    
        except Exception as e:
            logging.error(f"Error checking portfolio status: {e}")
