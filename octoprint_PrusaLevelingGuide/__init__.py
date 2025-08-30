# coding=utf-8
from __future__ import absolute_import


import time
import datetime
import octoprint.plugin
import octoprint.printer
import re

import flask


class PrusaLevelingGuidePlugin(octoprint.plugin.SimpleApiPlugin,
							octoprint.plugin.SettingsPlugin,
							octoprint.plugin.AssetPlugin,
							octoprint.plugin.TemplatePlugin,
							octoprint.plugin.StartupPlugin):
	
	
	def on_after_startup(self):
		self.bed_variance = None
		self.relative_values = []
		self.last_result = None
		# Old G81 line pattern: lines with only floats
		self._g81_line_regex = re.compile(r"^\s*([+-]?\d+\.\d+\s+)+[+-]?\d+\.\d+\s*$")
		# Generic float extractor (captures +0.123, -0.123)
		self._float_regex = re.compile(r"[+-]?\d+\.\d+")
		self.waiting_for_response = False
		self.sent_time = False
		# Storage for parsed numeric rows during a report
		self._mesh_rows = []
		self._expected_cols = None


	##~~ SimpleApiPlugin mixin
	def on_api_get(self, request):
		return flask.jsonify(bed_variance=self.bed_variance,
							values=self.relative_values,
							last_result=self.last_result)

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			# Default remains for MK3/MK3S+ (Einsy). For MK3.5/MK4 (xBuddy), use:
			# G28 ; home all\nM400\nG29 ; mesh bed leveling\nG29 T ; report mesh
			mesh_gcode = 'G28 W ; home all without mesh bed level\nM400\nG80; mesh bed leveling\nG81 ; check mesh leveling results',
			move_gcode = 'G1 Z60 Y210 F6000',
			enable_preheat = True,
			enable_preheat_nozzle = True,
			enable_preheat_bed = True,
			selected_profile = "",
			selected_view = "raw",
			view_type = "bed"
		)

	##~~ AssetPlugin mixin

	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/PrusaLevelingGuide.js"],
			css=["css/PrusaLevelingGuide.css"],
			less=["less/PrusaLevelingGuide.less"],
			photo_heatbed=["img/photo_headbed.png"]
		)

	##~~ Softwareupdate hook

	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		return dict(
			PrusaLevelingGuide=dict(
				displayName="Prusa Leveling Guide Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="scottrini",
				repo="OctoPrint-PrusaLevelingGuide",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/scottrini/OctoPrint-PrusaLevelingGuide/archive/{target_version}.zip"
			)
		)

	##~~ GCode Received hook
	
	def mesh_level_generate(self):
		"""Generate 3x3 relative values and variance from parsed mesh rows.
		Supports arbitrary N x N meshes (e.g., 7x7 from G81 or 21x21 from G29 T).
		"""
		try:
			if not self._mesh_rows:
				self._logger.debug("mesh_level_generate called with no rows; skipping")
				return
			rows_count = len(self._mesh_rows)
			cols_count = len(self._mesh_rows[0])
			# Validate all rows are equal length
			for r in self._mesh_rows:
				if len(r) != cols_count:
					self._logger.warning("Inconsistent row lengths in mesh: expected %d, got %d", cols_count, len(r))
					return
			mid_r = rows_count // 2
			mid_c = cols_count // 2
			# Sample 3x3 from corners/edges/center
			positions = [
				(0, 0), (0, mid_c), (0, cols_count - 1),
				(mid_r, 0), (mid_r, mid_c), (mid_r, cols_count - 1),
				(rows_count - 1, 0), (rows_count - 1, mid_c), (rows_count - 1, cols_count - 1)
			]
			center = self._mesh_rows[mid_r][mid_c]
			relative = [self._mesh_rows[r][c] - center for (r, c) in positions]
			self.relative_values = relative
			self.last_result = time.mktime(datetime.datetime.now().timetuple())
			self.bed_variance = round(max(relative) - min(relative), 3)
			self._logger.debug("Parsed mesh %dx%d, center=%.5f, variance=%.3f", rows_count, cols_count, center, self.bed_variance)
		finally:
			# Reset storage for next run
			self._mesh_rows[:] = []
			self._expected_cols = None

	def check_for_mesh_response(self, comm_instance, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
		"""Start waiting for a mesh report when the reporting command is sent.
		- MK3/MK3S+: 'G81' reports the mesh
		- MK3.5/MK4 (xBuddy): 'G29 T' reports the mesh; bare 'G29' only measures
		"""
		try:
			start_wait = False
			if gcode == "G81":
				start_wait = True
			elif gcode == "G29" and cmd and (" T" in cmd or cmd.strip().upper().endswith("T")):
				# 'G29 T' or 'G29 ... T'
				start_wait = True
			if start_wait:
				self.waiting_for_response = True
				self.sent_time = time.time()
				self._mesh_rows[:] = []
				self._expected_cols = None
				self._logger.debug("Waiting for mesh report after sending: %s", cmd)
		except Exception:
			# Be defensive; never break serial comm hook
			pass

	def mesh_level_check(self, comm, line, *args, **kwargs):
		# Only process when we are waiting for a report
		if not getattr(self, 'waiting_for_response', False):
			return line
		try:
			# Timeout guard (allow larger maps like 21x21)
			if self.sent_time and (time.time() - self.sent_time) > 120:
				self._logger.warning("Mesh report timed out after 120s; collected %d rows", len(self._mesh_rows))
				self.waiting_for_response = False
				self._mesh_rows[:] = []
				self._expected_cols = None
				return line

			text = line.strip()
			# Finalize on the ok that ends the report
			if text.lower().startswith('ok'):
				if self._mesh_rows:
					self.mesh_level_generate()
				self.waiting_for_response = False
				return line

			# Skip obviously unrelated lines
			if not text:
				return line

			# Prefer lines with row delimiter '|' (G29 T style)
			data_part = None
			if '|' in text:
				parts = text.split('|', 1)
				# Only consider the portion after the '|' (the numeric row)
				data_part = parts[1]
			else:
				# For G81 style, accept lines that are just floats separated by spaces
				if self._g81_line_regex.match(text):
					data_part = text

			if data_part is None:
				return line

			# Remove bracketed values like '[-0.060]' which appear in some reports
			data_part = re.sub(r"\[[^\]]+\]", "", data_part)
			# Avoid lines with colon labels (like 'X:')
			if ':' in data_part:
				return line

			floats = self._float_regex.findall(data_part)
			if not floats:
				return line

			row = [float(x) for x in floats]
			if self._expected_cols is None:
				self._expected_cols = len(row)
				# Sanity filter: ignore very short rows
				if self._expected_cols < 3:
					self._expected_cols = None
					return line
			# Only accept rows matching the expected column count
			if len(row) == self._expected_cols:
				self._mesh_rows.append(row)
				self._logger.debug("Captured mesh row %d/%s cols=%d", len(self._mesh_rows), '?', self._expected_cols)
		except Exception:
			# Always be safe in comm hook
			pass
		return line
	
			

__plugin_name__ = "Prusa Leveling Guide"
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PrusaLevelingGuidePlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
				"octoprint.comm.protocol.gcode.received": __plugin_implementation__.mesh_level_check,
				"octoprint.comm.protocol.gcode.sent": __plugin_implementation__.check_for_mesh_response
	}
