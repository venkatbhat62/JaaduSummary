# JaaduSummary
<h2>Summary</h2><br>
This tool automates the summarization of current or real time values in influxdb bucket(s) to new summary bucket(s) with pre-defined bucket name(s), and
clones the dashboard(s) that refer to the current bucket name in influxdb with corresponding summary bucket name(s). 
If the current dashboard refers to the libary panel(s) that refer to the current bucket name(s), it will create summary library panel(s) referring to the summary bucket name(s).
By running this tool on a daily basis via crontab, one can automate the summarization of data on a Hourly, Daily or Weekly basis with Min, Mean, and Max values for each summarization type. 
The bucket creation and summarization can be done for desired bucket names with Include or Exclude regex specs. Cloning of grafana dashboards can be also be controlled via IncludeDashboards and ExcludeDashboard specs.

JaaduSummary (https://docs.google.com/presentation/d/1ctKggZIkIrCY3Jl3BA_R63WU1RKhsJsGuB-LKLi_FkM/view) depicts the association pictorially.

<h2>Features</h2> 
<h3></h3>Version 1.00</h3>
<li>Accepts configuration file to customize the behavior to one needs via command line option (-c). Default config file name (if not passed as command line argument) - JACloneGrafanaDashboards.yml
<li>  Supports running the tool on remote host or on a host where grafana/Influxdb is/are running.
<li>  Configuration file supports customization per environment like Dev, Test, UAT, Prod by associating hostname; where the tool is running, to the environment.
<li>Can enable/disable the automatic creation of summary bucket for each current bucket name and data summarization.
<li>Automatic bucket management can be limited to the desired buckets via IncludeBuckets and ExcludeBuckets specifications.
<li>Supports grouping of summary data based on periodicity like Hourly, Daily and Weekly with separate retention policies for each group.
<li>  This is to allow shorter duration retention for Hourly or Daily summary buckets and longer retention for Daily or Weekly summary data.
<li>Summarized data for each group can have Min, Mean, and Max values within the summarization interval. This enables one to visualize separate dashboards for each value type.
<li>Creates log file at current working directory on a per day basis. 

<h2>Installation</h2>
<li>Copy the config file and the script to the folder on target host and add a crontab to run it on a daily basis.
<li>There is NO log file purge mechanism available yet. Suggest adding another crontab to delete log file older than disired window.
