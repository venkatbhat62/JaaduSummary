# JaaduSummary
Summary
This tool automates the summarization of current or real time values in influxdb bucket(s) to new summary bucket(s) with pre-defined bucket name(s), 
clones the dashboard(s) that refer to the current bucket name in influxdb with corresponding summary bucket name(s). 
If the current dashboard refers to the libary panel(s) that refer to the current bucket name(s), it will create summary library panel(s) referring to the summary bucket name(s).
By running this tool on a daily basis via crontab, one can automate the summarization of data on a Hourly, Daily or Weekly basis with Min, Mean, and Max values for each summarization type. 
The bucket creation and summarization can be done for desired bucket names with Include or Exclude regex specs.

<a href:https://docs.google.com/presentation/d/1ctKggZIkIrCY3Jl3BA_R63WU1RKhsJsGuB-LKLi_FkM/view>JaaduSummary</a> depicts the association pictorially.

